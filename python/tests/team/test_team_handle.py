"""The operator's handle (spec/api.json Team; design §4.4): start and send as operator requests in
the team log, their idempotency keys, the busy bound, and the pure reads members() and events().
Every test ends with the team's replay check. Mirrors TypeScript's test/team/handle.test.ts."""

import asyncio
from collections.abc import AsyncIterator, Sequence

from team.run_kit import say, sq_of, types
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    MemberRef,
    Principal,
    Store,
    Team,
    TeamAgent,
    TeamCursor,
    TeamItem,
    TeamRef,
    agent,
    open_team,
    scripted_model,
    sqlite,
)
from threads.agents.member_results import MemberCompleted
from threads.agents.store import now_ms
from threads.agents.team_handle import HandleEnv
from threads.agents.team_handle_types import (
    EpochRestarted,
    OperatorSource,
    TeamEvent,
    TeamSendRefused,
    TeamStartRefused,
)
from threads.agents.team_log_mail import take_team_log_mail
from threads.agents.team_tools import Sent, Started
from threads.log import BranchId, Event, OperatorRequestEvent
from threads.result import Err, Ok
from threads.team.rebuild import rebuild_team_index
from threads.team.rows import team_row

BOB = Principal(issuer="api", tenant="local", subject="bob")
OPERATOR = Principal(issuer="api", tenant="local", subject="operator")
"""The run's own principal: the local operator run() records by default."""


def _lead(writer: Sequence[str] = ("Draft.",)) -> TeamAgent[None, str]:
    member = agent(name="writer", model=scripted_model({"responses": [say(t) for t in writer]}))
    script = [say("Ready."), say("Noted."), say("Noted.")]
    return agent(name="lead", model=scripted_model({"responses": script}), team=[member])


async def _ran(store: Store) -> tuple[TeamAgent[None, str], Completed[str], Team]:
    lead = _lead()
    r = await lead.run("Get ready.", store=store)
    assert isinstance(r, Completed)
    return lead, r, r.team


async def _team_log(store: Store, team: Team) -> Sequence[Event]:
    sq = await sq_of(store)
    row = await sq.run(lambda c: team_row(c, team.ref.id))
    assert row is not None
    read = await sq.read(BranchId(row.team_log_branch_id), now_ms())
    assert isinstance(read, Ok)
    return read.value.fold.events


def _ref(team: Team, name: str) -> MemberRef:
    return MemberRef(tenant=team.ref.tenant, team=team.ref.id, name=name, generation=1)


async def _collect(items: AsyncIterator[TeamItem]) -> list[TeamItem]:
    return [item async for item in items]


def test_team_start_is_one_operator_request_the_member_starts_with_its_label() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        started = await team.start("writer", "Draft the summary.", label="drafter")
        assert started == Started(_ref(team, "writer-1"))
        assert types(await _team_log(store, team)) == [
            "team_opened",
            "operator_request",
            "message_policy_decided",
            "member_started",
            "message_sent",
        ]
        members = await team.members()
        assert [(m.name, m.state, m.label) for m in members] == [
            ("lead", "idle", None),
            ("writer-1", "starting", "drafter"),
        ]
        assert members[0].result == MemberCompleted(_ref(team, "lead"), "Ready.")
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_the_leads_next_run_materializes_and_runs_an_operators_member() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead, r, team = await _ran(store)
        await team.start("writer", "Draft the summary.")
        await lead.run("Continue.", store=store, thread=r.thread)
        writer = next(m for m in await team.members() if m.name == "writer-1")
        assert writer.state == "idle"
        assert writer.result == MemberCompleted(_ref(team, "writer-1"), "Draft.")
        # Its task notification goes to the team log, which takes it as a receipt only.
        sq = await sq_of(store)
        await take_team_log_mail(sq, team.ref.id, None)
        assert types(await _team_log(store, team)).count("message_received") == 1
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_an_unknown_agent_and_a_member_not_started_yet_are_refused_and_logged() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        assert await team.start("editor", "Go.") == TeamStartRefused("unknown_agent")
        sent = await team.send(_ref(team, "writer-1"), "Hello.")
        assert sent == TeamSendRefused("unknown_member")
        assert types(await _team_log(store, team)).count("operator_refused") == 2  # noqa: PLR2004
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_team_send_reaches_a_started_member_once_its_key_replays_the_outcome() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        await team.start("writer", "Draft.")
        writer = _ref(team, "writer-1")
        first = await team.send(writer, "Keep it short.", idempotency_key="s1")
        assert isinstance(first, Sent)
        before = len(await _team_log(store, team))
        # The caller never saw the outcome: the same key, principal and body replay it.
        assert await team.send(writer, "Keep it short.", idempotency_key="s1") == first
        assert len(await _team_log(store, team)) == before
        # Another body under the key is refused, recorded without the key.
        reused = await team.send(writer, "Longer.", idempotency_key="s1")
        assert reused == TeamSendRefused("idempotency_key_reused")
        bob = await open_team(store, team.ref, principal=BOB)
        assert isinstance(bob, Ok)
        mismatch = await bob.value.send(writer, "Keep it short.", idempotency_key="s1")
        assert mismatch == TeamSendRefused("idempotency_key_principal_mismatch")
        keys = [
            e.data.idempotency_key
            for e in await _team_log(store, team)
            if isinstance(e, OperatorRequestEvent)
        ]
        assert [k if isinstance(k, str) else None for k in keys] == [
            None,
            "s1",
            None,
            None,
        ]
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_a_held_team_log_lease_past_the_bound_is_busy_and_nothing_is_recorded() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        sq = await sq_of(store)
        row = await sq.run(lambda c: team_row(c, team.ref.id))
        assert row is not None
        held = await sq.acquire(BranchId(row.team_log_branch_id), "someone-else", now_ms)
        assert isinstance(held, Ok)
        bounded = Team(HandleEnv(sq, team.ref, OPERATOR, None, busy_bound_ms=30))
        before = len(await _team_log(store, team))
        assert await bounded.start("writer", "Go.") == TeamStartRefused("busy")
        assert len(await _team_log(store, team)) == before
        await held.value.release()
        # Released: the same request goes through (this handle has no definition of the lead).
        assert await bounded.start("writer", "Go.") == TeamStartRefused("unknown_agent")
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_another_principal_of_the_tenant_acts_through_its_own_handle() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        opened = await open_team(store, team.ref, principal=BOB)
        assert isinstance(opened, Ok)
        assert isinstance(await opened.value.start("writer", "Draft."), Started)
        request = next(
            e for e in await _team_log(store, team) if isinstance(e, OperatorRequestEvent)
        )
        assert request.actor.principal == BOB
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_another_tenant_is_forbidden_and_an_unknown_team_is_not_found() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        other = Principal(issuer="api", tenant="globex", subject="m")
        forbidden = await open_team(store, team.ref, principal=other)
        assert isinstance(forbidden, Err)
        assert forbidden.error.code == "forbidden"
        unknown = TeamRef("local", "0192c000-0000-7000-8000-00000000ffff")
        missing = await open_team(store, unknown, principal=OPERATOR)
        assert isinstance(missing, Err)
        assert missing.error.code == "not_found"

    asyncio.run(main())


def test_the_feed_team_opened_the_leads_events_then_the_operators_request() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        await team.start("writer", "Draft.")
        items = await _collect(team.events())
        kinds = [f"{i.source.kind}:{i.event.type}" for i in items if isinstance(i, TeamEvent)]
        # A new team's feed starts with team_opened, then the lead's events (design §5).
        assert kinds[:2] == ["team:team_opened", "member:thread_started"]
        assert kinds[-4:] == [
            "operator:operator_request",
            "operator:message_policy_decided",
            "operator:member_started",
            "operator:message_sent",
        ]
        last = items[-1]
        assert isinstance(last, TeamEvent)
        assert isinstance(last.source, OperatorSource)
        assert last.source.principal == OPERATOR
        # Resuming after a cursor yields exactly the rest, each once.
        rest = await _collect(team.events(after=items[2].cursor))
        assert rest == items[3:]
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_a_rebuilt_feed_restarts_epoch_restarted_then_the_whole_new_epoch() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        _, _, team = await _ran(store)
        before = await _collect(team.events())
        sq = await sq_of(store)
        assert isinstance(await rebuild_team_index(sq, team.ref.id), Ok)
        after = await _collect(team.events(after=before[-1].cursor))
        assert after[0] == EpochRestarted(TeamCursor(2, 0))
        ids = {i.event.event_id for i in after if isinstance(i, TeamEvent)}
        assert ids == {i.event.event_id for i in before if isinstance(i, TeamEvent)}
        assert len(after) == len(before) + 1
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())
