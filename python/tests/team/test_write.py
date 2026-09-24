"""The index hooks on a live writer: a lead's first append opens its team whole, a failing hook
rolls the append back, every appended event gets one feed row, and mail.claim's CAS."""

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable
from dataclasses import asdict

from team.team_kit import CASES, LEAD, TEAM, TEAM_LOG, TENANT, assert_team_replays, branch_of
from team.team_kit import verified as read
from team.writes import ReplayClock, draft_of

from threads.log import Event
from threads.result import Ok
from threads.store import SqliteStore, Writer
from threads.team.claim import claim_mail
from threads.team.constants import TEAM_CONSTANTS

T0 = 1_790_000_000_000
LEAD_BRANCH = branch_of(LEAD)
LOG_BRANCH = branch_of(TEAM_LOG)
TASK = f"{LEAD_BRANCH}:c1"


def _lead_events() -> list[Event]:
    log = read((CASES / "team-settle-wakes-lead" / "logs" / "lead.jsonl").read_bytes())
    assert isinstance(log, Ok)
    return list(log.value.fold.events)


LEAD_EVENTS = _lead_events()


def _run(test: Callable[[SqliteStore, ReplayClock], Awaitable[None]]) -> None:
    async def main() -> None:
        opened = await SqliteStore.open(tenant_id=TENANT)
        assert isinstance(opened, Ok)
        try:
            await test(opened.value, ReplayClock(T0))
        finally:
            await opened.value.close()

    asyncio.run(main())


async def _open_lead(store: SqliteStore, clock: ReplayClock, n: int) -> Writer:
    """The lead's log opened with its first `n` recorded events."""
    drafts = [draft_of(e) for e in LEAD_EVENTS[:n]]
    opened = await store.open_branch(LEAD, LEAD_BRANCH, drafts, holder_id="lead", clock=clock)
    assert isinstance(opened, Ok)
    assert isinstance(opened.value, Writer)
    return opened.value


def _rows(sql: str) -> Callable[[sqlite3.Connection], list[tuple[object, ...]]]:
    return lambda conn: conn.execute(sql).fetchall()


def test_a_leads_first_append_opens_the_team_log_the_teams_row_and_its_row() -> None:
    async def test(store: SqliteStore, clock: ReplayClock) -> None:
        await _open_lead(store, clock, 2)
        log = await store.read(LOG_BRANCH, clock())
        assert isinstance(log, Ok)
        (opened,) = log.value.fold.events
        lead = {"tenant": TENANT, "team": TEAM, "name": "lead", "generation": 1}
        assert json.loads(opened.model_dump_json(include={"type", "data"})) == {
            "type": "team_opened",
            "data": {"team": TEAM, "lead": lead, "lead_thread_id": LEAD},
        }
        teams = await store.run(_rows("SELECT * FROM teams"))
        assert teams == [(TEAM, TENANT, LEAD, LOG_BRANCH, None)]
        members = await store.run(_rows("SELECT name, generation, role, state FROM team_members"))
        assert members == [("lead", 1, "lead", "running")]
        feed = await store.run(
            _rows("SELECT feed_offset, branch_id, seq FROM team_feed ORDER BY feed_offset")
        )
        assert feed == [(1, LOG_BRANCH, 1), (2, LEAD_BRANCH, 1), (3, LEAD_BRANCH, 2)]
        # The team log's lease is free: the next writer takes it at once, at epoch 2.
        taken = await store.acquire(LOG_BRANCH, "operator", clock)
        assert isinstance(taken, Ok)
        assert [taken.value.epoch] == [2]
        await assert_team_replays(store, TEAM)

    _run(test)


def test_a_team_log_that_already_exists_refuses_the_append_and_none_of_it_is_written() -> None:
    async def test(store: SqliteStore, clock: ReplayClock) -> None:
        assert await store.create(TEAM_LOG, LOG_BRANCH, clock()) == Ok(None)
        drafts = [draft_of(e) for e in LEAD_EVENTS[:2]]
        refused = await store.open_branch(LEAD, LEAD_BRANCH, drafts, holder_id="lead", clock=clock)
        assert not isinstance(refused, Ok)
        assert refused.error.code == "invalid_transition"
        for table in ("teams", "team_members", "team_feed"):
            assert await store.run(_rows(f"SELECT * FROM {table}")) == []  # noqa: S608
        assert not isinstance(await store.branch(LEAD_BRANCH), Ok)

    _run(test)


def test_the_feed_gets_one_row_per_appended_event_in_commit_order() -> None:
    async def test(store: SqliteStore, clock: ReplayClock) -> None:
        writer = await _open_lead(store, clock, 2)
        for e in LEAD_EVENTS[2:10]:
            assert isinstance(await writer.append([draft_of(e)]), Ok)
        feed = await store.run(
            lambda c: c.execute(
                "SELECT feed_offset, seq FROM team_feed WHERE branch_id = ? ORDER BY feed_offset",
                (LEAD_BRANCH,),
            ).fetchall()
        )
        assert [seq for _, seq in feed] == list(range(1, 11))
        assert [offset for offset, _ in feed] == list(range(2, 12))
        await assert_team_replays(store, TEAM)

    _run(test)


def test_one_worker_claims_a_pending_row_until_its_claim_expires() -> None:
    async def test(store: SqliteStore, clock: ReplayClock) -> None:
        await _open_lead(store, clock, 9)
        t, ttl = clock(), TEAM_CONSTANTS.claim_ttl_ms
        assert await store.run(lambda c: claim_mail(c, TASK, "a", t)) == "claimed"
        assert await store.run(lambda c: claim_mail(c, TASK, "b", t + 1)) == "busy"
        assert await store.run(lambda c: claim_mail(c, TASK, "b", t + ttl - 1)) == "busy"
        assert await store.run(lambda c: claim_mail(c, TASK, "b", t + ttl, 10)) == "claimed"
        claims = await store.run(_rows("SELECT claim_token, claim_expires_at FROM mail"))
        assert claims == [("b", t + ttl + 10)]

    _run(test)


def test_a_row_that_is_not_pending_or_does_not_exist_is_never_claimed() -> None:
    async def test(store: SqliteStore, clock: ReplayClock) -> None:
        await _open_lead(store, clock, 9)
        await store.run(lambda c: c.execute("UPDATE mail SET state = 'consumed'"))
        assert await store.run(lambda c: claim_mail(c, TASK, "a", clock())) == "busy"
        assert await store.run(lambda c: claim_mail(c, "no-such-mail", "a", clock())) == "busy"

    _run(test)


def test_the_teams_constants_are_the_ones_the_op_vectors_pin() -> None:
    vectors = json.loads((CASES.parent / "vectors" / "team-ops.json").read_text())
    assert vectors["constants"] == asdict(TEAM_CONSTANTS)
