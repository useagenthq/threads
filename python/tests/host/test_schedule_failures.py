"""A failing schedule, agent or thread is reported and never stops the others: every other
schedule is reserved and decided, recovery always runs, and the scheduler loop keeps going."""

import asyncio
import contextlib
from datetime import UTC, datetime
from itertools import chain, repeat

import pytest
from pydantic import JsonValue

from threads import agent, extension, scripted_model, sqlite
from threads.agents.agent import Agent
from threads.agents.store import Store, now_ms, open_store
from threads.host import Schedule
from threads.host.runs import Runner
from threads.host.schedules import Scheduler
from threads.result import Ok
from threads.store import Draft

REPLY: JsonValue = {
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
NINE = int(datetime(2026, 5, 1, 9, 0, tzinfo=UTC).timestamp() * 1000)
DAY = 86_400_000
DAILY = Schedule(id="daily", agent="good", cron="0 9 * * *", input="Hi.")
CORRUPT = (
    "INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, thread_id,"
    " claimed_at, agent, input_json, timezone) VALUES ('local', 'other', 1, 'pending',"
    " 'not-a-thread-id', 1, 'good', '\"Hi.\"', 'UTC')"
)


def broken(name: str) -> Agent[None, str]:
    async def fail() -> None:
        raise RuntimeError("no creds")

    return agent(
        name=name,
        model=scripted_model({"responses": []}),
        extensions=[extension(name="boot", setup=fail)],
    )


def healthy(name: str) -> Agent[None, str]:
    return agent(name=name, model=scripted_model({"responses": [REPLY] * 3}))


async def occurrences(store: Store) -> list[tuple[str, str]]:
    sq = await open_store(store)
    return await sq.run(
        lambda c: c.execute(
            "SELECT schedule_id, state FROM schedule_occurrences"
            " ORDER BY schedule_id, occurrence_at"
        ).fetchall()
    )


def test_a_broken_agents_schedule_is_reported_and_a_healthy_one_fires_on_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        runner = Runner(store, {"bad": broken("bad"), "good": healthy("good")}, {})
        bad = Schedule(id="broken", agent="bad", cron="0 9 * * *", input="Hi.")
        await Scheduler(runner, [bad, DAILY]).tick(NINE - 60_000, NINE + 1_000)
        await runner.settled()
        assert await occurrences(store) == [("daily", "fired")]
        await runner.stop()

    asyncio.run(main())
    assert "schedule broken" in capsys.readouterr().err


def test_a_corrupt_pending_row_is_reported_and_recovery_still_resumes_an_open_turn() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        runner = Runner(store, {"good": healthy("good")}, {})
        scheduler = Scheduler(runner, [DAILY])
        await scheduler.tick(NINE - 60_000, NINE + 1_000)
        await runner.settled()
        sq = await open_store(store)
        (thread,) = await sq.tables.schedules.threads()
        root = await sq.root(thread)
        assert isinstance(root, Ok)
        # A run cut short: an input durable, its turn open, no run going.
        writer = await sq.acquire(root.value, "crashed", now_ms)
        assert isinstance(writer, Ok)
        actor: dict[str, JsonValue] = {
            "kind": "user",
            "principal": {"issuer": "api", "tenant": "local", "subject": "op"},
        }
        busy = Draft("user_input", {"source": "api", "text": "Busy."}, actor)
        assert isinstance(await writer.value.append([busy]), Ok)
        await writer.value.release()
        await sq.run(lambda c: c.execute(CORRUPT))
        await scheduler.tick(NINE - 60_000, NINE + DAY - 60_000)
        await runner.settled()
        read = await sq.read(root.value, now_ms())
        assert isinstance(read, Ok)
        assert not read.value.fold.in_turn
        await runner.stop()

    asyncio.run(main())


def test_the_scheduler_loop_reports_a_corrupt_row_and_keeps_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        runner = Runner(store, {"good": healthy("good")}, {})
        await (await open_store(store)).run(lambda c: c.execute(CORRUPT))
        times = chain([NINE - 60_000], repeat(NINE + 1_000))
        running = asyncio.create_task(Scheduler(runner, [DAILY], lambda: next(times)).run())
        await asyncio.sleep(0.2)
        assert not running.done()
        running.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await running
        await runner.stop()

    asyncio.run(main())
    assert "schedule rows are corrupt" in capsys.readouterr().err
