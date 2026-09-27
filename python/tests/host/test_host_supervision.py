"""Supervision of a host member (lane 29E, section E): the host team log's writer decides once on
each ended generation, a restart opens the next one in a new empty thread on the pin this host
holds now, and a stop is final until an operator starts it.

A host member ends failed only on a rebind failure, and the way to cause one here is the real one:
the definition behind a name changes while its row still names the pin it started on, so the next
run of that generation cannot rebind it. Mirrors TypeScript's host/test/host-supervision.test.ts.
"""

import asyncio
import contextlib
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx
from host.host_members_kit import ALICE, events_of, rule
from host.test_http import bearer, run, start
from pydantic.experimental.missing_sentinel import MISSING
from team.run_kit import Answering, answers, call, reply_to, say
from team.team_kit import assert_team_replays

from threads import Store, agent, sqlite
from threads.agents.store import open_store, scoped
from threads.agents.team_handle_types import TeamSendRefused, TeamStartRefused
from threads.agents.team_tools import Started
from threads.host import host
from threads.host.members import HostMemberOptions
from threads.log import (
    MailRefusedEvent,
    MemberRef,
    MemberStartedEvent,
    SupervisorDecidedEvent,
)
from threads.result import Ok
from threads.store.conn import Conn
from threads.store.sql import int_of, text_of
from threads.team.host_team import host_team_ids
from threads.team.rows import MemberRow, member_rows

if TYPE_CHECKING:
    from pydantic import JsonValue

    from threads.agents.factory import Agent
    from threads.host.app import Host

IDS = host_team_ids(ALICE.tenant)
NEXT = 2
"""The generation a first restart opens."""
MEMBERS: Mapping[str, HostMemberOptions] = {"billing": HostMemberOptions()}
RULES = [rule("support", "billing", ["ask"])]


def support_asking() -> "Agent[None, str]":
    """A caller that asks billing once per turn and then answers its user. Its answer is made
    from the request it is given rather than from a position in a script, so two runs of one
    host never race each other for the next scripted response."""

    def next_(request: str) -> "JsonValue":
        if "c1" in request:
            return say("Billing answered.")
        return call("c1", "ask", {"to": "billing", "question": "Is INV-1001 paid?"})

    return agent(name="support", model=Answering(next_))


def billing_saying(note: str) -> "Agent[None, str]":
    """billing as one host defines it; `note` changes its pin without changing its behaviour."""

    def replying(request: str) -> "JsonValue":
        return reply_to("r1", request, "Paid.")

    def done(_request: str) -> "JsonValue":
        return say("Answered.")

    return agent(
        name="billing", instructions=f"You are billing. {note}", model=answers([replying, done])
    )


@contextlib.asynccontextmanager
async def hosted(
    store: Store, note: str, members: Mapping[str, HostMemberOptions]
) -> AsyncGenerator[tuple["Host", httpx.AsyncClient]]:
    """One host on this store, with billing pinned on `note`, and its HTTP client."""
    agents = {"support": support_asking(), "billing": billing_saying(note)}
    served = host(
        store=store, agents=agents, authenticate=bearer, message_policy=RULES, members=members
    )
    async with served:
        transport = httpx.ASGITransport(app=served.asgi)
        async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
            yield served, client


async def members_of(store: Store) -> list[MemberRow]:
    sq = await open_store(scoped(store, ALICE.tenant))
    rows = await sq.run(lambda c: member_rows(c, IDS.team))
    return [r for r in rows if r.team_id == IDS.team]


async def decisions_of(store: Store) -> list[SupervisorDecidedEvent]:
    log = await events_of(store, IDS.log_branch_id)
    return [e for e in log if isinstance(e, SupervisorDecidedEvent)]


def _mail(conn: Conn) -> list[tuple[str, int | None]]:
    rows = conn.execute(
        "SELECT state, to_generation FROM mail WHERE team_id = ?", (IDS.team,)
    ).fetchall()
    return [(text_of(s), None if g is None else int_of(g)) for s, g in rows]


def _branches(conn: Conn) -> list[str]:
    rows = conn.execute(
        "SELECT branch_id FROM branches WHERE tenant_id = ?", (ALICE.tenant,)
    ).fetchall()
    return [text_of(b) for (b,) in rows]


async def staleness(store: Store) -> tuple[list[str], int]:
    """Every mail row's state, and every stale_member refusal anywhere in the tenant."""
    sq = await open_store(scoped(store, ALICE.tenant))
    states = [state for state, _ in await sq.run(_mail)]
    refusals = 0
    for branch in await sq.run(_branches):
        for event in await events_of(store, branch):
            if isinstance(event, MailRefusedEvent) and event.data.code == "stale_member":
                refusals += 1
    return states, refusals


async def until(check: Callable[[], Awaitable[bool]], tries: int = 400) -> None:
    for _ in range(tries):
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timed out waiting")


async def pending(store: Store) -> int:
    sq = await open_store(scoped(store, ALICE.tenant))
    return sum(1 for state, _ in await sq.run(_mail) if state == "pending")


async def quiet(store: Store) -> None:
    """Nothing of the host team is in flight: no pending mail, and every generation settled.
    The replay check reads the index, so it has to be still while it does."""

    async def settled() -> bool:
        rows = await members_of(store)
        states = {r.state for r in rows}
        return bool(rows) and await pending(store) == 0 and states <= {"idle", "ended"}

    await until(settled)


async def replays(store: Store) -> None:
    await quiet(store)
    sq = await open_store(scoped(store, ALICE.tenant))
    await assert_team_replays(sq, IDS.team)


@contextlib.asynccontextmanager
async def drifted(
    members: Mapping[str, HostMemberOptions],
) -> AsyncGenerator[tuple["Host", httpx.AsyncClient, Store]]:
    """A store whose host team is open with billing at generation 1, idle, pinned on the
    definition one host had — and a second, live host that defines billing differently."""
    store = sqlite(":memory:")
    async with hosted(store, "v1", members) as (_first, client):
        # The tenant's host team opens on its first run, so one caller run of alice's brings
        # generation 1 into being; settled, not merely materialized.
        await asks(client, "k-0")

        async def idle() -> bool:
            rows = await members_of(store)
            return bool(rows) and rows[0].state == "idle" and await pending(store) == 0

        await until(idle)
    async with hosted(store, "v2", members) as (second, client2):
        yield second, client2, store


async def asks(client: httpx.AsyncClient, key: str) -> None:
    """Starts a run of support, which asks billing once. Its own answer is not what these tests
    read, so it is started and left to the host, as TypeScript's does."""
    body: JsonValue = {"agent": "support", "input": "Is INV-1001 paid?"}
    answered = await start(client, "alice", key, body)
    assert answered.status_code == HTTPStatus.ACCEPTED, answered.text


def test_a_failed_rebind_restarts_into_a_new_empty_thread_on_the_current_pin() -> None:
    async def main() -> None:
        async with drifted(MEMBERS) as (_h, client, store):
            first = (await members_of(store))[0]
            await asks(client, "k-1")
            await until(lambda: _decided(store))
            await until(lambda: _generations(store, 2))

            all_ = await decisions_of(store)
            assert len(all_) == 1
            one = all_[0]
            assert one.data.action == "restart"
            assert one.data.restarts_in_window == 0
            assert one.data.member.generation == 1
            # Its `ended` is generation 1's own member_ended, by position in that log.
            assert one.data.ended.branch_id == first.branch_id

            log = await events_of(store, IDS.log_branch_id)
            restarted = [e for e in log if isinstance(e, MemberStartedEvent)][-1]
            assert restarted.data.restart_of == 1
            assert restarted.data.member.generation == NEXT
            # Re-pinned: the new generation runs the definition this host has, not the failed one.
            assert restarted.data.config_hash != first.config_hash

            rows = await members_of(store)
            assert [(r.generation, r.state) for r in rows] == [(1, "ended"), (2, "idle")]
            # Decision 6's completeness, which no conformance case pins: the end append is the
            # last word of that generation. After member_ended come only the refusals and
            # bounces it owes, and nothing terminates the member a second time.
            own1 = [e.type for e in await events_of(store, first.branch_id or "")]
            after = own1[own1.index("member_ended") + 1 :]
            assert set(after) == {"mail_refused", "message_sent"}
            assert rows[1].thread_id != first.thread_id
            # Erlang-style: a restarted member has fresh state, so its log is its opening alone.
            own = await events_of(store, rows[1].branch_id or "")
            assert [e.type for e in own] == ["thread_started"]
            await replays(store)

    run(main)


def test_restart_never_records_a_stop_and_the_name_stays_ended() -> None:
    async def main() -> None:
        members = {"billing": HostMemberOptions(restart="never")}
        async with drifted(members) as (_h, client, store):
            await asks(client, "k-1")
            await until(lambda: _decided(store))
            all_ = await decisions_of(store)
            assert len(all_) == 1
            assert all_[0].data.action == "stop"
            assert all_[0].data.policy.restart == "never"
            # No second generation: every end is on record, and this one stops the name.
            assert [r.generation for r in await members_of(store)] == [1]
            await replays(store)

    run(main)


def test_an_ask_across_a_restart_is_returned_and_never_stale() -> None:
    async def main() -> None:
        async with drifted(MEMBERS) as (_h, client, store):
            await asks(client, "k-1")
            await until(lambda: _decided(store))
            await until(lambda: _generations(store, 2))

            # The end refused the pending ask itself, so its row is returned, not stale.
            states, refusals = await staleness(store)
            assert refusals == 0
            assert "stale" not in states
            assert "returned" in states

            # A second caller binds the current generation in its own transaction.
            await asks(client, "k-2")

            async def answered() -> bool:
                rows = await members_of(store)
                own = await events_of(store, rows[1].branch_id or "")
                return any(e.type == "message_sent" for e in own)

            await until(answered)
            states, refusals = await staleness(store)
            assert refusals == 0
            assert "stale" not in states
            await replays(store)

    run(main)


def test_a_cancel_ends_the_member_cancelled_and_the_supervisor_stops_it() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with hosted(store, "v1", MEMBERS) as (served, client):
            await asks(client, "k-0")

            async def idle() -> bool:
                rows = await members_of(store)
                return bool(rows) and rows[0].state == "idle" and await pending(store) == 0

            await until(idle)
            team = await served.team(principal=ALICE)
            assert isinstance(team, Ok), team
            rows = await members_of(store)
            got = await team.value.cancel(_ref(rows[0]))
            assert got.status == "cancel_requested"
            await until(lambda: _decided(store))
            all_ = await decisions_of(store)
            assert len(all_) == 1
            # A cancel is a stop: it is what an operator asked for, never undone by a restart.
            assert all_[0].data.action == "stop"
            assert [r.generation for r in await members_of(store)] == [1]
            await replays(store)

    run(main)


def test_after_a_stop_mail_is_refused_and_team_start_opens_the_next_generation() -> None:
    async def main() -> None:
        members = {"billing": HostMemberOptions(restart="never")}
        async with drifted(members) as (served, client, store):
            await asks(client, "k-1")
            await until(lambda: _decided(store))
            team = await served.team(principal=ALICE)
            assert isinstance(team, Ok), team
            handle = team.value
            ended = (await members_of(store))[0]
            # The row is ended, so a send to the name is refused where it is sent.
            sent = await handle.send(_ref(ended), "Are you there?")
            assert isinstance(sent, TeamSendRefused), sent
            assert sent.code == "member_ended"

            started = await handle.start("billing")
            assert isinstance(started, Started), started
            assert started.member.generation == NEXT
            log = await events_of(store, IDS.log_branch_id)
            restarted = [e for e in log if isinstance(e, MemberStartedEvent)][-1]
            assert restarted.data.restart_of == 1
            # An operator's restart names the operator_request that asked for it (rule 51).
            provenance = restarted.data.provenance
            assert provenance is not MISSING, restarted
            assert provenance.root_request.thread_id == IDS.log_thread_id
            await until(lambda: _generations(store, 2))
            await replays(store)

    run(main)


def test_a_name_that_is_no_host_member_and_a_live_generation_are_forbidden() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        async with hosted(store, "v1", MEMBERS) as (served, client):
            await asks(client, "k-0")
            await until(lambda: _generations(store, 1))
            team = await served.team(principal=ALICE)
            assert isinstance(team, Ok), team
            missing = await team.value.start("payroll")
            assert isinstance(missing, TeamStartRefused), missing
            assert missing.code == "forbidden"
            # Nothing to restart while the current generation is live.
            live = await team.value.start("billing")
            assert isinstance(live, TeamStartRefused), live
            assert live.code == "forbidden"
            await replays(store)

    run(main)


def _ref(row: MemberRow) -> MemberRef:
    return MemberRef(tenant=ALICE.tenant, team=IDS.team, name=row.name, generation=row.generation)


async def _decided(store: Store) -> bool:
    return bool(await decisions_of(store))


async def _generations(store: Store, count: int) -> bool:
    return len(await members_of(store)) >= count
