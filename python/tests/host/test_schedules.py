"""Schedules: cron in an IANA zone, and an occurrence claimed once before its
schedule_fired, so two schedulers that see the same due minute start one run."""

import asyncio
from datetime import UTC, datetime

import pytest
from pydantic import JsonValue

from threads import ConfigError, agent, scripted_model, sqlite
from threads.agents.store import open_store
from threads.host import Schedule, host
from threads.host.runs import Runner
from threads.host.schedules import Scheduler, parse_cron
from threads.log import ScheduleFiredEvent, UserInputEvent
from threads.result import Ok

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


def test_an_occurrence_is_claimed_once_and_runs_on_its_own_thread() -> None:
    schedule = Schedule(id="digest", agent="bot", cron="0 9 * * *", input="Send the digest.")

    async def main() -> None:
        store = sqlite(":memory:")
        bot = agent(model=scripted_model({"responses": [REPLY]}))
        runner = Runner(store, {"bot": bot}, {})
        at = int(datetime(2026, 9, 23, 9, 0, tzinfo=UTC).timestamp() * 1000)
        first, second = Scheduler(runner, [schedule]), Scheduler(runner, [schedule])
        assert await first.fire(schedule, at)
        assert not await second.fire(schedule, at)
        await runner.stop()
        sq = await open_store(store)
        rows = await sq.run(
            lambda c: c.execute("SELECT thread_id FROM schedule_occurrences").fetchall()
        )
        ((thread,),) = rows
        root = await sq.root(thread)
        assert isinstance(root, Ok)
        read = await sq.read(root.value, 0)
        assert isinstance(read, Ok)
        events = read.value.fold.events
        (fired,) = [e for e in events if isinstance(e, ScheduleFiredEvent)]
        assert (fired.data.schedule_id, fired.data.scheduled_for) == ("digest", at)
        (entered,) = [e for e in events if isinstance(e, UserInputEvent)]
        assert (entered.data.source, entered.data.delivery_event_id) == ("schedule", fired.event_id)

    asyncio.run(main())


def test_ready_refuses_a_schedule_of_an_unknown_agent() -> None:
    bad = Schedule(id="x", agent="nobody", cron="* * * * *", input="hi")
    served = host(store=sqlite(":memory:"), agents={}, schedules=[bad])
    with pytest.raises(ConfigError):
        asyncio.run(served.ready())
