"""The host's team tick, driven one pass at a time (design §7 Phase 2 A, and the 29A crash
drills): a claim another host took leaves the mail alone until it expires, the consume that wakes
an idle lead happens exactly once, a turn it left open is resumed by the host with no second turn,
and the operator's cancel reaches a member through the host's worker alone. The same cases as
TypeScript's host/test/team-drills.test.ts."""

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from host.test_api_recovery import until
from team.run_kit import call, say, sq_of
from team.team_kit import assert_team_replays

from threads import (
    Completed,
    MemberRef,
    Principal,
    Store,
    TeamAgent,
    TeamRef,
    agent,
    open_team,
    scripted_model,
    sqlite,
)
from threads.agents.store import now_ms, open_store
from threads.agents.team_answers import CancelRequested
from threads.host import host
from threads.host.runs import Runner
from threads.host.teams import Teams
from threads.log import BranchId, Event, MessageReceivedEvent, TurnCompletedEvent, UserInputEvent
from threads.result import Ok
from threads.store import SqliteStore

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")


@dataclass(frozen=True, slots=True)
class Seeded:
    """A lead that ran once, with a helper the operator then started, and no host anywhere."""

    store: Store
    lead: TeamAgent[None, str]
    team: str
    branch: BranchId


async def seeded(name: str) -> Seeded:
    helper = agent(
        name=f"{name}_helper",
        model=scripted_model(
            {
                "responses": [
                    call("s1", "send", {"to": f"{name}_lead", "text": "The scan is clean."}),
                    say("Reported."),
                ]
            }
        ),
    )
    lead = agent(
        name=f"{name}_lead",
        model=scripted_model({"responses": [say("Standing by."), say("The scan is clean.")]}),
        team=[helper],
    )
    store = sqlite(":memory:")
    ran = await lead.run("Stand by.", store=store)
    assert isinstance(ran, Completed), ran
    started = await ran.team.start(f"{name}_helper", "Scan the deps.")
    assert started.status == "started", started
    return Seeded(store, lead, ran.team.ref.id, ran.thread.branch)


def driver(seed: Seeded) -> tuple[Teams, Runner]:
    """A team tick of its own, as the host's runs it, but one pass at a time."""
    runner = Runner(seed.store, {"lead": seed.lead}, {})
    return Teams(runner), runner


async def _events(seed: Seeded) -> Sequence[Event]:
    read = await (await open_store(seed.store)).read(seed.branch, now_ms())
    assert isinstance(read, Ok), read
    return read.value.fold.events


def _received(events: Sequence[Event]) -> int:
    return sum(1 for e in events if isinstance(e, MessageReceivedEvent))


def _turns(events: Sequence[Event]) -> int:
    return sum(1 for e in events if isinstance(e, TurnCompletedEvent))


async def _mail(sq: SqliteStore) -> list[tuple[object, ...]]:
    return await sq.run(
        lambda c: c.execute(
            "SELECT mail_id, state, to_name FROM mail ORDER BY created_at"
        ).fetchall()
    )


async def _to_lead(sq: SqliteStore, name: str) -> str:
    """The mail waiting for the lead, once its helper has sent it."""

    async def sent() -> bool:
        return any(row[1] == "pending" and row[2] == f"{name}_lead" for row in await _mail(sq))

    await until(sent)
    row = next(row for row in await _mail(sq) if row[2] == f"{name}_lead")
    assert isinstance(row[0], str)
    return row[0]


def test_a_claim_taken_before_the_lease_waits_for_the_claim_to_expire() -> None:
    async def main() -> None:
        seed = await seeded("claim")
        teams, runner = driver(seed)
        sq = await sq_of(seed.store)
        try:
            await teams.once()
            mail_id = await _to_lead(sq, "claim")

            # A host claimed the row and died before taking the lease: nothing else consumes it.
            await sq.run(
                lambda c: c.execute(
                    "UPDATE mail SET claim_token = 'dead-host', claim_expires_at = ? "
                    "WHERE mail_id = ?",
                    (now_ms() + 60_000, mail_id),
                )
            )
            await teams.once()
            assert _received(await _events(seed)) == 0

            # The claim runs out (its TTL is 30 s): the next pass consumes it, once.
            await sq.run(
                lambda c: c.execute(
                    "UPDATE mail SET claim_expires_at = 0 WHERE mail_id = ?", (mail_id,)
                )
            )
            await teams.once()
            assert _received(await _events(seed)) == 1
            await teams.once()
            assert _received(await _events(seed)) == 1
            assert [row[1] for row in await _mail(sq) if row[0] == mail_id] == ["consumed"]
        finally:
            await teams.stop()
            await runner.stop()
        await assert_team_replays(sq, seed.team)

    asyncio.run(main())


def test_a_crash_after_the_consume_resumes_the_turn_with_no_second_turn() -> None:
    async def main() -> None:
        seed = await seeded("resume")
        teams, runner = driver(seed)
        sq = await sq_of(seed.store)
        await teams.once()
        await _to_lead(sq, "resume")
        # The consume opens the lead's turn; the host that made it never ran it.
        await teams.once()
        assert _received(await _events(seed)) == 1
        assert _turns(await _events(seed)) == 1
        await teams.stop()
        await runner.stop()

        async with host(store=seed.store, agents={"lead": seed.lead}) as served:
            assert served is not None

            async def woke() -> bool:
                return _turns(await _events(seed)) == 2  # noqa: PLR2004 - the woken turn

            await until(woke)
            all_events = await _events(seed)
            assert _turns(all_events) == 2  # noqa: PLR2004 - the woken turn, once
            assert _received(all_events) == 1
            assert sum(1 for e in all_events if isinstance(e, UserInputEvent)) == 1
        await assert_team_replays(sq, seed.team)

    asyncio.run(main())


def test_the_operators_cancel_of_a_member_is_applied_by_the_hosts_worker() -> None:
    async def main() -> None:
        seed = await seeded("cancel")
        opened = await open_team(
            seed.store, TeamRef(OPERATOR.tenant, seed.team), principal=OPERATOR
        )
        assert isinstance(opened, Ok), opened
        team = opened.value
        member = MemberRef(
            tenant=OPERATOR.tenant, team=seed.team, name="cancel_helper-1", generation=1
        )
        assert isinstance(await team.cancel(member), CancelRequested)

        teams, runner = driver(seed)
        try:
            await teams.once()

            async def ended() -> bool:
                return any(
                    m.name == "cancel_helper-1" and m.state == "ended" for m in await team.members()
                )

            await until(ended)
        finally:
            await teams.stop()
            await runner.stop()
        settled = next(m for m in await team.members() if m.name == "cancel_helper-1")
        assert settled.result is not None
        assert settled.result.status == "cancelled"
        await assert_team_replays(await sq_of(seed.store), seed.team)

    asyncio.run(main())


def test_stop_leaves_every_row_durable_and_the_next_host_finishes_the_work() -> None:
    async def main() -> None:
        seed = await seeded("stopped")
        teams, runner = driver(seed)
        sq = await sq_of(seed.store)
        await teams.once()
        await _to_lead(sq, "stopped")
        await teams.stop()
        await runner.stop()
        assert _received(await _events(seed)) == 0
        # Nothing is claimed for good: the row is still pending, with its envelope intact.
        assert any(row[1] == "pending" for row in await _mail(sq))

        async with host(store=seed.store, agents={"lead": seed.lead}) as served:
            assert served is not None

            async def woke() -> bool:
                return _turns(await _events(seed)) == 2  # noqa: PLR2004 - the woken turn

            await until(woke)
            assert _received(await _events(seed)) == 1
        await assert_team_replays(sq, seed.team)

    asyncio.run(main())
