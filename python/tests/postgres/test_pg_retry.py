"""Retry safety (lane 27, review 27-1): a serialization failure forced at the worst point of an
attempt re-runs the whole transaction once, the outcome, log bytes and rows equal the unforced
run, and the writer is not poisoned. Forced past the retry budget, the result is SQLite's
busy timeout's (StoreError) and nothing is written."""

import asyncio
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass

import pytest
from pg_drill import Forcing, opened_with
from pg_kit import Leg, need_postgres
from store.test_writer import DONE, ROOT, T0, THREAD, Clock, started, user

from threads.log import ParseError
from threads.result import Ok
from threads.store import Draft, StoreError, Writer, verify_export
from threads.store import conn as seam
from threads.store.conn import Conn
from threads.store.verify import StoredEvent
from threads.store.writer import DecideTx, Refusal

type At = Callable[[str], bool]

OLD_ATTEMPTS = 3
"""Retries the old fixed schedule allowed (four attempts)."""


@pytest.fixture
def leg() -> Iterator[Leg]:
    made = Leg(need_postgres())
    yield made
    made.close()


def at(prefix: str) -> At:
    return lambda sql: sql.startswith(prefix)


def _rows(conn: Conn) -> list[tuple[object, ...]]:
    return conn.execute(
        "SELECT branch_id, child_thread_id FROM pending_wakes ORDER BY child_thread_id"
    ).fetchall()


def _alongside(conn: Conn, _events: Sequence[StoredEvent]) -> ParseError | None:
    conn.execute(
        "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, 'alongside')", (ROOT,)
    )
    return None


def _decide(tx: DecideTx) -> Sequence[Draft] | Refusal[str]:
    tx.conn.execute(
        "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, 'decided')", (ROOT,)
    )
    return [user("decided"), DONE]


type Scenario = Callable[[Writer], Awaitable[object]]


async def _append(w: Writer) -> object:
    return await w.append([user("hi"), DONE], _alongside)


async def _decided(w: Writer) -> object:
    return await w.append_decided(_decide)


@dataclass(frozen=True)
class Run:
    outcome: object
    after: object
    exported: bytes
    rows: list[tuple[object, ...]]
    retries: int


def _forced(leg: Leg, scenario: Scenario, point: At | None, times: int) -> Run:
    """The scenario on a fresh store, forced `times` at `point`; then one more append."""

    async def main() -> Run:
        store, conn = await opened_with(leg, Forcing)
        clock = Clock()
        writer = await started(store, clock)
        if point is not None:
            conn.force(point, times)
        try:
            outcome: object = await scenario(writer)
        except StoreError as error:
            outcome = type(error)
        conn.force(lambda _sql: False, 0)
        after = await writer.append([user("after")])
        exported = await store.export(ROOT)
        rows = await store.run(_rows, read_only=True)
        await store.close()
        assert isinstance(exported, Ok)
        return Run(outcome, after, exported.value, rows, conn.retries)

    return asyncio.run(main())


@pytest.mark.parametrize("scenario", [_append, _decided], ids=["append", "decided"])
@pytest.mark.parametrize(
    "point",
    [at("INSERT INTO events"), at("UPDATE branches SET head_seq"), at("COMMIT")],
    ids=["after admission", "after the index", "at commit, after decide and alongside"],
)
def test_one_forced_failure_retries_once_and_equals_the_unforced_run(
    leg: Leg, scenario: Scenario, point: At
) -> None:
    unforced = _forced(leg, scenario, None, 0)
    forced = _forced(leg, scenario, point, 1)
    assert forced.retries == 1
    assert _shape(forced) == _shape(unforced)
    assert isinstance(forced.after, Ok)  # the writer is not poisoned: it appends at the next seq


def _shape(run: Run) -> tuple[object, ...]:
    """What a run did, event ids and hashes aside (they are fresh per run)."""
    assert isinstance(run.outcome, Ok)
    read = verify_export(run.exported, T0)
    assert isinstance(read, Ok)
    events = [(e.seq, e.type, e.epoch) for e, _ in read.value.segments[-1].events]
    return events, run.rows


@pytest.mark.parametrize("scenario", [_append, _decided], ids=["append", "decided"])
def test_failures_past_the_retry_budget_are_an_outage_and_write_nothing(
    leg: Leg, scenario: Scenario, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short budget: a drill that always conflicts ends in 100 ms, not the 5 s default.
    monkeypatch.setattr(seam, "RETRY_BUDGET_S", 0.1)
    run = _forced(leg, scenario, at("INSERT INTO events"), 10_000)
    assert run.outcome is StoreError
    assert run.retries > OLD_ATTEMPTS  # more than the old four attempts fit in the budget
    assert run.rows == []
    # As after SQLite's busy timeout, the writer that met the outage stays poisoned.
    assert not isinstance(run.after, Ok)
    assert b"decided" not in run.exported
    assert b'"hi"' not in run.exported


def test_a_forced_lease_acquire_retries_and_takes_the_next_epoch(leg: Leg) -> None:
    async def main() -> None:
        store, conn = await opened_with(leg, Forcing)
        clock = Clock()
        first = await started(store, clock)
        await first.release()
        conn.force(at("INSERT INTO leases"))
        taken = await store.acquire(ROOT, "b", clock)
        assert isinstance(taken, Ok)
        assert taken.value.epoch == first.epoch + 1
        assert conn.retries == 1
        assert isinstance(await taken.value.append([user("hi")]), Ok)
        await store.close()

    asyncio.run(main())


def test_a_forced_team_rebuild_retries_and_rebuilds_the_same_rows(leg: Leg) -> None:
    from team.team_kit import TEAM, TENANT, add, case_logs, index_rows  # noqa: PLC0415

    from threads.team.rebuild import rebuild_team_index  # noqa: PLC0415

    async def main() -> None:
        store, conn = await opened_with(leg, Forcing, tenant=TENANT)
        await add(store, case_logs("team-settle-wakes-lead"))
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        clean = await store.run(index_rows, read_only=True)
        conn.force(at("INSERT INTO team_feed"))
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        assert conn.retries == 1
        assert await store.run(index_rows, read_only=True) == clean
        await store.close()

    asyncio.run(main())


def test_a_read_only_transaction_that_writes_fails_as_a_bug(leg: Leg) -> None:
    async def main() -> None:
        store, _ = await opened_with(leg, Forcing)
        assert await store.create(THREAD, ROOT, T0) == Ok(None)
        with pytest.raises(Exception, match="read-only"):
            await store.run(
                lambda c: c.execute("DELETE FROM leases WHERE branch_id = ?", (ROOT,)),
                read_only=True,
            )
        await store.close()

    asyncio.run(main())
