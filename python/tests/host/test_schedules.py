"""Cron parsing and ready() checks, and the single winner of a pending occurrence. The schedule
lifecycle is replayed from the shared vector in test_schedule_threads.py."""

import asyncio
import contextlib
from datetime import UTC, datetime
from itertools import chain, repeat

import pytest
from pydantic import JsonValue

from threads import ConfigError, agent, extension, scripted_model, sqlite
from threads.agents.run import pinned_start
from threads.agents.store import Store, now_ms, open_store
from threads.host import Schedule, host
from threads.host.occurrences import log_occurrence
from threads.host.runs import Runner
from threads.host.schedules import Scheduler, parse_cron
from threads.log import ScheduleFiredEvent, ScheduleSkippedEvent
from threads.result import Ok
from threads.store.retention import delete_thread
from threads.store.schedules import Due

USAGE: JsonValue = {"input_tokens": 1, "output_tokens": 1}
REPLY: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": USAGE,
}


def test_cron_fields_steps_ranges_and_day_rules() -> None:
    weekday_hours = parse_cron("*/15 9-17 * * 1-5")
    assert weekday_hours.matches(datetime(2026, 9, 23, 9, 30, tzinfo=UTC))
    assert not weekday_hours.matches(datetime(2026, 9, 23, 9, 31, tzinfo=UTC))
    assert not weekday_hours.matches(datetime(2026, 9, 27, 9, 30, tzinfo=UTC))
    # Both day fields restricted: either one matching is enough (Vixie cron).
    either = parse_cron("0 0 1 * 0")
    assert either.matches(datetime(2026, 9, 27, 0, 0, tzinfo=UTC))
    assert either.matches(datetime(2026, 10, 1, 0, 0, tzinfo=UTC))
    for bad in ("* * *", "61 * * * *", "a * * * *", "*/0 * * * *"):
        with pytest.raises(ConfigError):
            parse_cron(bad)


def test_two_schedulers_holding_one_pending_row_the_stale_one_appends_nothing() -> None:
    schedule = Schedule(id="daily", agent="bot", cron="0 9 * * *", input="Report.")
    nine = int(datetime(2026, 5, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
    day = 86_400_000

    def runner(store: Store) -> Runner:
        return Runner(
            store, {"bot": agent(name="bot", model=scripted_model({"responses": [REPLY] * 2}))}, {}
        )

    async def main() -> None:
        store = sqlite(":memory:")
        a = runner(store)
        await Scheduler(a, [schedule]).tick(nine - 60_000, nine + 1_000)
        await a.settled()
        sq = await open_store(store)
        (thread,) = await sq.tables.schedules.threads()
        root = await sq.root(thread)
        assert isinstance(root, Ok)
        # An outbound delivery holds the writer when the next occurrence falls due: it stays
        # pending.
        outbound = await sq.acquire(root.value, "outbound", now_ms)
        assert isinstance(outbound, Ok)
        await Scheduler(a, [schedule]).tick(nine - 60_000, nine + day + 1_000)
        await outbound.value.release()
        # After a restart, two schedulers both select it; one decides it first.
        (stale,) = await sq.tables.schedules.pending()
        b = runner(store)
        await Scheduler(b, [schedule]).tick(nine + day + 60_000, nine + day + 60_000)
        await b.settled()
        writer = await sq.acquire(root.value, "stale-scheduler", now_ms)
        assert isinstance(writer, Ok)
        assert not await log_occurrence(writer.value, "local", stale, None)
        await writer.value.release()
        read = await sq.read(root.value, now_ms())
        assert isinstance(read, Ok)
        occurrences = ScheduleFiredEvent | ScheduleSkippedEvent
        logged = [type(e) for e in read.value.fold.events if isinstance(e, occurrences)]
        assert logged == [ScheduleFiredEvent, ScheduleFiredEvent]
        states = await sq.run(
            lambda c: c.execute(
                "SELECT state, logged_seq IS NOT NULL FROM schedule_occurrences"
                " ORDER BY occurrence_at"
            ).fetchall()
        )
        assert states == [("fired", 1), ("fired", 1)]
        await a.stop()
        await b.stop()

    asyncio.run(main())


NINE = int(datetime(2026, 5, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
DAY = 86_400_000
DAILY = Schedule(id="daily", agent="bot", cron="0 9 * * *", input="Report.")


def test_a_deletion_before_a_reservation_strands_nothing_and_a_retired_key_stays_retired() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(name="bot", model=scripted_model({"responses": [REPLY] * 2}))
        runner = Runner(store, {"bot": bot}, {})
        scheduler = Scheduler(runner, [DAILY])
        await scheduler.tick(NINE - 60_000, NINE + 1_000)
        await runner.settled()
        sq = await open_store(store)
        rows = sq.tables.schedules
        (thread,) = await rows.threads()
        root = await sq.root(thread)
        assert isinstance(root, Ok)
        outbound = await sq.acquire(root.value, "outbound", now_ms)
        assert isinstance(outbound, Ok)
        await scheduler.tick(NINE - 60_000, NINE + DAY + 1_000)
        await outbound.value.release()
        await sq.run(lambda c: delete_thread(c, "local", thread, now_ms()))
        # The retired key and a new one, reserved after the deletion committed: the retired row
        # stays retired, and the new one lands on a new thread, never the deleted one.
        started = await pinned_start(bot.definition, store)
        due = [Due("daily", NINE + n * DAY, "bot", "Report.", "UTC", missed=False) for n in (1, 2)]
        await rows.reserve_due(started, due, now_ms())
        (identity,) = await rows.threads()
        assert identity != thread
        found = await sq.run(
            lambda c: c.execute(
                "SELECT state, thread_id = ? FROM schedule_occurrences ORDER BY occurrence_at",
                (identity,),
            ).fetchall()
        )
        assert found == [("fired", 0), ("retired", 0), ("pending", 1)]
        await runner.stop()

    asyncio.run(main())


def test_an_unreadable_schedule_thread_fails_the_reservation_and_keeps_its_identity() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(name="bot", model=scripted_model({"responses": [REPLY]}))
        runner = Runner(store, {"bot": bot}, {})
        await Scheduler(runner, [DAILY]).tick(NINE - 60_000, NINE + 1_000)
        await runner.settled()
        sq = await open_store(store)
        rows = sq.tables.schedules
        (thread,) = await rows.threads()
        await sq.run(lambda c: c.execute("UPDATE events SET line = x'00' WHERE seq = 1"))
        started = await pinned_start(bot.definition, store)
        due = [Due("daily", NINE + DAY, "bot", "Report.", "UTC", missed=False)]
        with pytest.raises(TypeError, match="can't be read"):
            await rows.reserve_due(started, due, now_ms())
        assert await rows.threads() == (thread,)
        count = await sq.run(
            lambda c: c.execute("SELECT count(*) FROM schedule_occurrences").fetchone()
        )
        assert count == (1,)
        await runner.stop()

    asyncio.run(main())


def test_a_stored_pending_row_whose_input_is_not_an_input_is_reported_as_corrupt() -> None:
    async def main() -> None:
        sq = await open_store(sqlite(":memory:"))
        await sq.run(
            lambda c: c.execute(
                "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
                " thread_id, claimed_at, agent, input_json, timezone) VALUES ('local', 'daily', 1,"
                " 'pending', '0192a000-0000-7000-8000-000000000001', 1, 'bot',"
                " '{\"not\": \"an input\"}', 'UTC')"
            )
        )
        with pytest.raises(TypeError, match="schedule rows are corrupt"):
            await sq.tables.schedules.pending()

    asyncio.run(main())


def test_a_setup_failure_decides_nothing_and_writes_no_thread() -> None:
    async def broken() -> None:
        raise RuntimeError("no creds")

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(
            name="bot",
            model=scripted_model({"responses": [REPLY]}),
            extensions=[extension(name="boot", setup=broken)],
        )
        runner = Runner(store, {"bot": bot}, {})
        with pytest.raises(ConfigError, match="setup failed"):
            await Scheduler(runner, [DAILY]).tick(NINE - 60_000, NINE + 1_000)
        sq = await open_store(store)
        counts = await sq.run(
            lambda c: c.execute(
                "SELECT (SELECT count(*) FROM schedule_threads),"
                " (SELECT count(*) FROM schedule_occurrences), (SELECT count(*) FROM threads)"
            ).fetchone()
        )
        assert counts == (0, 0, 0)
        await runner.stop()

    asyncio.run(main())


def test_a_setup_failure_is_reported_and_the_scheduler_keeps_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def broken() -> None:
        raise RuntimeError("no creds")

    async def main() -> None:
        bot = agent(
            name="bot",
            model=scripted_model({"responses": []}),
            extensions=[extension(name="boot", setup=broken)],
        )
        runner = Runner(sqlite(":memory:"), {"bot": bot}, {})
        # Ready a minute before 09:00, then every reading is just after it: the first pass is due.
        times = chain([NINE - 60_000], repeat(NINE + 1_000))
        running = asyncio.create_task(Scheduler(runner, [DAILY], lambda: next(times)).run())
        await asyncio.sleep(0.2)
        assert not running.done()
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running

    asyncio.run(main())
    assert "schedule tick failed" in capsys.readouterr().err


def test_ready_refuses_a_schedule_of_an_unknown_agent() -> None:
    bad = Schedule(id="x", agent="nobody", cron="* * * * *", input="hi")
    served = host(store=sqlite(":memory:"), agents={}, schedules=[bad])
    with pytest.raises(ConfigError):
        asyncio.run(served.ready())


def test_ready_refuses_a_schedule_id_that_is_not_a_name() -> None:
    bot = agent(name="bot", model=scripted_model({"responses": []}))
    bad = Schedule(id="daily-digest", agent="bot", cron="* * * * *", input="hi")
    served = host(store=sqlite(":memory:"), agents={"bot": bot}, schedules=[bad])
    with pytest.raises(ConfigError, match="lowercase letters, digits and underscores"):
        asyncio.run(served.ready())


def test_a_stored_pending_row_whose_thread_id_is_not_a_uuid_is_reported_as_corrupt() -> None:
    async def main() -> None:
        sq = await open_store(sqlite(":memory:"))
        await sq.run(
            lambda c: c.execute(
                "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state,"
                " thread_id, claimed_at, agent, input_json, timezone) VALUES ('local', 'daily', 1,"
                " 'pending', 'not-a-thread-id', 1, 'bot', '\"Report.\"', 'UTC')"
            )
        )
        with pytest.raises(TypeError, match="schedule rows are corrupt"):
            await sq.tables.schedules.pending()

    asyncio.run(main())
