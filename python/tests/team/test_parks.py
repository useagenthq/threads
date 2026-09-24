"""A member that parks (design §2.7.1): its first park sends its starter one member_parked notice;
the lead parks on the member and run() returns parked. Once what the member waits on is answered,
running the lead again runs the member on, and its settlement resumes the lead. Mirrors
TypeScript's test/team/parks.test.ts."""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from pydantic import BaseModel
from team.run_kit import call, events, member_events, receipts, say, sq_of, start, types
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    Parked,
    Principal,
    RunContext,
    Store,
    TeamAgent,
    TeamRunResult,
    agent,
    scripted_model,
    sqlite,
    tool,
)
from threads.agents.run import execute
from threads.log import ThreadId
from threads.result import Ok
from threads.team.rows import member_rows
from threads.thread.handle import open_thread

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


class Mail(BaseModel):
    to: str


@dataclass(frozen=True, slots=True)
class _Team:
    store: Store
    sent: list[str]
    lead: TeamAgent[None, str]
    r: TeamRunResult[str]
    approve: Callable[[], Awaitable[None]]


async def _parked_team(lead_answers: Sequence[str]) -> _Team:
    """A lead whose researcher's email needs an approval; its run has returned parked."""
    store = sqlite(":memory:")
    sent: list[str] = []

    async def send_email(args: Mail, _ctx: RunContext[None]) -> str:
        sent.append(args.to)
        return "sent"

    mail = tool(
        name="send_email", description="Send an email.", input=Mail, runs="host", execute=send_email
    )
    researcher = agent(
        name="researcher",
        model=scripted_model(
            {"responses": [call("m1", "send_email", {"to": "bob"}), say("Mailed bob.")]}
        ),
        tools=[mail],
    )
    answers = [say(a) for a in lead_answers]
    lead = agent(
        name="lead",
        model=scripted_model(
            {"responses": [start("c1", "researcher", "Mail bob."), say("Started."), *answers]}
        ),
        team=[researcher],
    )
    r = await lead.run("Get bob mailed.", store=store)

    async def approve() -> None:
        sq = await sq_of(store)
        rows = await sq.run(lambda c: member_rows(c, r.team.ref.id))
        row = next(m for m in rows if m.name == "researcher-1")
        handle = await open_thread(store, ThreadId(row.thread_id))
        assert isinstance(handle, Ok)
        pending = await handle.value.pending_approvals()
        assert isinstance(pending, Ok)
        approved = await handle.value.approve(pending.value[0].challenge_id, OPERATOR)
        assert isinstance(approved, Ok), approved

    return _Team(store, sent, lead, r, approve)


def test_parks_its_lead_run_returns_parked_on_the_member_with_one_notice() -> None:
    async def main() -> None:
        t = await _parked_team([])
        r = t.r
        assert isinstance(r, Parked), r
        assert r.reason == "awaiting_member"
        assert r.pending[0].id.startswith(f"{r.thread.branch}:")
        member = await member_events(t.store, r.team.ref.id, "researcher-1")
        # One notice for the member's first park, in the park's own append.
        parked = types(member).index("parked")
        assert types(member)[parked + 1] == "message_sent"
        assert len(receipts(await events(t.store, r.thread), "member_parked")) == 1
        await assert_team_replays(await sq_of(t.store), r.team.ref.id)

    asyncio.run(main())


def test_after_the_members_approval_the_hosts_recovery_runs_it_on_and_resumes_the_lead() -> None:
    async def main() -> None:
        t = await _parked_team(["The researcher mailed bob."])
        await t.approve()
        # The host's run of the lead, with no new input.
        resumed = await execute(
            t.lead.definition,
            None,
            {"store": t.store, "principal": OPERATOR, "thread": t.r.thread},
            None,
            lambda _e: None,
        )
        assert isinstance(resumed, Completed), resumed
        assert resumed.output == "The researcher mailed bob."
        assert t.sent == ["bob"]
        lead_log = await events(t.store, t.r.thread)
        assert len(receipts(lead_log, "member_settled")) == 1
        assert "resumed" in types(lead_log)
        await assert_team_replays(await sq_of(t.store), t.r.team.ref.id)

    asyncio.run(main())


def test_after_the_members_approval_a_new_input_first_resumes_the_lead_then_starts_its_turn() -> (
    None
):
    async def main() -> None:
        t = await _parked_team(["The researcher mailed bob.", "Nothing else."])
        await t.approve()
        after = await t.lead.run("Anything else?", store=t.store, thread=t.r.thread)
        assert isinstance(after, Completed), after
        assert after.output == "Nothing else."
        assert t.sent == ["bob"]
        inputs = [e for e in await events(t.store, t.r.thread) if e.type == "user_input"]
        assert len(inputs) == 2  # noqa: PLR2004 - the first run's and this one's
        await assert_team_replays(await sq_of(t.store), t.r.team.ref.id)

    asyncio.run(main())
