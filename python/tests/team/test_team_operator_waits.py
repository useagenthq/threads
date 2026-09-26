"""The operator's ask, wait, cancel and ask_status (spec/api.json Team; spec/schema/README.md,
"Teams", the operator's handle): each writing method is one operator request in the team log; ask
and wait return the outcome the team log records, driving the team until it does. The same cases
as TypeScript's test/team/operator-waits.test.ts."""

import asyncio
from collections.abc import Sequence

from pydantic import JsonValue
from team.run_kit import answers, reply_to, say, sq_of, types
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    MemberRef,
    Model,
    Principal,
    Store,
    Team,
    TeamAgent,
    agent,
    open_team,
    scripted_model,
    sqlite,
)
from threads.agents.member_results import MemberCancelled, MemberCompleted
from threads.agents.store import now_ms
from threads.agents.team_answers import Answered, CancelRequested, Waited
from threads.agents.team_handle_types import (
    AskNotFound,
    TeamAskRefused,
    TeamCancelRefused,
    TeamWaitRefused,
)
from threads.log import BranchId, Event
from threads.result import Ok
from threads.team.rows import team_row


async def _ran(writer: Model, reader: Model | None = None) -> tuple[Store, Team]:
    """A lead that ran once and listed `writer` (and `reader`), and its team's handle."""
    store, team, _lead = await _ran_held(writer, reader)
    return store, team


async def _ran_held(
    writer: Model, reader: Model | None = None, name: str = "lead"
) -> tuple[Store, Team, TeamAgent[None, str]]:
    """As _ran, with the lead: open_team binds a lead this process still holds. A lead name of its
    own keeps open_team from binding another test's lead of the same config."""
    store = sqlite(":memory:")
    team = [agent(name="writer", model=writer)]
    if reader is not None:
        team.append(agent(name="reader", model=reader))
    lead = agent(name=name, model=scripted_model({"responses": [say("Ready.")]}), team=team)
    r = await lead.run("Get ready.", store=store)
    assert isinstance(r, Completed)
    return store, r.team, lead


def _ref(team: Team, name: str) -> MemberRef:
    return MemberRef(tenant=team.ref.tenant, team=team.ref.id, name=name, generation=1)


async def _team_log(store: Store, team: Team) -> Sequence[Event]:
    sq = await sq_of(store)
    row = await sq.run(lambda c: team_row(c, team.ref.id))
    assert row is not None
    read = await sq.read(BranchId(row.team_log_branch_id), now_ms())
    assert isinstance(read, Ok)
    return read.value.fold.events


def test_the_members_reply_is_the_asks_outcome_ask_status_reads_the_same() -> None:
    async def main() -> None:
        writer = answers(
            [
                lambda _r: say("Drafted."),
                lambda request: reply_to("r1", request, "Batteries."),
                lambda _r: say("Replied."),
            ]
        )
        store, team = await _ran(writer)
        await team.start("writer", "Draft the summary.")
        ref = _ref(team, "writer-1")
        got = await team.ask(ref, "Which topic?")
        assert isinstance(got, Answered)
        assert (got.text, got.member) == ("Batteries.", ref)
        assert await team.ask_status(got.ask_id) == got
        assert "ask_closed" in types(await _team_log(store, team))
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_no_reply_by_the_deadline_is_timed_out_a_retry_under_its_key_reattaches() -> None:
    async def main() -> None:
        store, team = await _ran(answers([lambda _r: say("Drafted."), lambda _r: say("No idea.")]))
        await team.start("writer", "Draft.")
        ref = _ref(team, "writer-1")
        first = await team.ask(ref, "Which topic?", timeout_ms=300, idempotency_key="ask-1")
        assert first.status == "timed_out"
        before = len(await _team_log(store, team))
        again = await team.ask(ref, "Which topic?", timeout_ms=300, idempotency_key="ask-1")
        assert again == first
        assert len(await _team_log(store, team)) == before
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_ask_status_of_an_ask_the_team_log_never_sent_is_not_found() -> None:
    async def main() -> None:
        _store, team = await _ran(answers([]))
        ask_id = "0192b000-0000-7000-8000-0000000000ff:nope"
        assert await team.ask_status(ask_id) == AskNotFound(ask_id)

    asyncio.run(main())


def test_an_ask_to_the_lead_is_refused_lead_and_waiting_for_it_still_works() -> None:
    async def main() -> None:
        store, team = await _ran(answers([]))
        lead = _ref(team, "lead")
        assert await team.ask(lead, "Which topic?") == TeamAskRefused("lead")
        assert types(await _team_log(store, team))[-3:] == [
            "operator_request",
            "message_policy_decided",
            "operator_refused",
        ]
        # wait and cancel on the lead are meaningful and stay.
        waited = await team.wait([lead], timeout_ms=3000)
        assert isinstance(waited, Waited)
        assert not waited.timed_out
        assert isinstance(await team.cancel(lead), CancelRequested)
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_no_members_or_a_mode_below_one_is_invalid_request_and_records_nothing() -> None:
    async def main() -> None:
        store, team = await _ran(answers([lambda _r: say("Drafted.")]))
        await team.start("writer", "Draft.")
        ref = _ref(team, "writer-1")
        before = len(await _team_log(store, team))
        assert await team.wait([]) == TeamWaitRefused("invalid_request")
        assert await team.wait([ref], mode=0) == TeamWaitRefused("invalid_request")
        assert len(await _team_log(store, team)) == before

    asyncio.run(main())


def test_a_ref_carrying_another_tenant_names_no_member_of_this_team() -> None:
    async def main() -> None:
        _store, team = await _ran(answers([lambda _r: say("Drafted.")]))
        await team.start("writer", "Draft.")
        foreign = MemberRef(tenant="globex", team=team.ref.id, name="writer-1", generation=1)
        assert await team.cancel(foreign) == TeamCancelRefused("unknown_member")

    asyncio.run(main())


def test_a_handle_from_open_team_drives_the_team_as_the_runs_handle_does() -> None:
    async def main() -> None:
        store, team, lead = await _ran_held(answers([lambda _r: say("Drafted.")]), name="opener")
        bob = Principal(issuer="api", tenant="local", subject="bob")
        opened = await open_team(store, team.ref, principal=bob)
        assert isinstance(opened, Ok)
        await opened.value.start("writer", "Draft.")
        ref = _ref(team, "writer-1")
        got = await opened.value.wait([ref])
        assert isinstance(got, Waited)
        assert got.finished == (MemberCompleted(ref, "Drafted."),)
        assert lead.name == "opener"  # held until here
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_waits_for_a_member_to_settle_with_its_result() -> None:
    async def main() -> None:
        store, team = await _ran(answers([lambda _r: say("Drafted.")]))
        await team.start("writer", "Draft.")
        ref = _ref(team, "writer-1")
        got = await team.wait([ref])
        assert got == Waited("waited", (MemberCompleted(ref, "Drafted."),), (), (), False)
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_mode_any_returns_once_one_member_settles_a_mode_above_the_count_records_nothing() -> None:
    async def main() -> None:
        store, team = await _ran(
            answers([lambda _r: say("Drafted.")]), answers([lambda _r: say("Read.")])
        )
        await team.start("writer", "Draft.")
        await team.start("reader", "Read.")
        both = [_ref(team, "writer-1"), _ref(team, "reader-1")]
        before = len(await _team_log(store, team))
        assert await team.wait(both, mode=3) == TeamWaitRefused("invalid_request")
        assert len(await _team_log(store, team)) == before
        got = await team.wait(both, mode="any")
        assert isinstance(got, Waited)
        assert got.finished
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_a_starting_members_cancel_is_durable_at_once_it_ends_cancelled_without_running() -> None:
    async def main() -> None:
        requests: list[str] = []

        def drafted(request: str) -> JsonValue:
            requests.append(request)
            return say("Drafted.")

        store, team = await _ran(answers([drafted]))
        await team.start("writer", "Draft.")
        ref = _ref(team, "writer-1")
        assert await team.cancel(ref) == CancelRequested(ref)
        got = await team.wait([ref])
        assert isinstance(got, Waited)
        assert got.finished == (MemberCancelled(ref),)
        assert requests == []
        assert await team.cancel(ref) == TeamCancelRefused("member_ended")
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())


def test_the_operators_cancel_of_a_member_the_team_never_had_is_unknown_member() -> None:
    async def main() -> None:
        store, team = await _ran(answers([]))
        got = await team.cancel(_ref(team, "nobody-1"))
        assert got == TeamCancelRefused("unknown_member")
        assert types(await _team_log(store, team))[-3:] == [
            "operator_request",
            "message_policy_decided",
            "operator_refused",
        ]
        await assert_team_replays(await sq_of(store), team.ref.id)

    asyncio.run(main())
