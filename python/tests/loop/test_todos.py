"""todo_write through agent.run (F8, ): the list is a todos_updated event read back
by Thread.todos(); a malformed list or repeated ids is an error result with nothing appended;
the reminder shows open items once after ten turns without a todo_write."""

import asyncio
from collections.abc import Sequence

from pydantic import JsonValue

from threads import Completed, Thread, agent, scripted_model, sqlite
from threads.log import Event, InjectedEvent, TodosUpdatedEvent, ToolResultEvent
from threads.loop.todos import REMIND_AFTER
from threads.result import Ok

USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
ITEMS: JsonValue = [
    {"id": "1", "content": "Add the column", "status": "in_progress"},
    {"id": "2", "content": "Backfill", "status": "pending", "active_form": "Backfilling"},
]


def text(reply: str) -> JsonValue:
    return {"content": [{"type": "text", "text": reply}], "stop_reason": "end_turn", "usage": USAGE}


def write(todos: JsonValue, call_id: str = "call_1") -> JsonValue:
    part: JsonValue = {
        "type": "tool_use",
        "call_id": call_id,
        "name": "todo_write",
        "input": {"todos": todos},
    }
    return {"content": [part], "stop_reason": "tool_use", "usage": USAGE}


async def events_of(thread: Thread) -> list[Event]:
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    return [e.event for e in timeline.value.entries]


async def run(responses: Sequence[JsonValue]) -> tuple[Thread, list[Event]]:
    bot = agent(model=scripted_model({"responses": list(responses)}))
    result = await bot.run("plan", store=sqlite(":memory:"))
    assert isinstance(result, Completed)
    return result.thread, await events_of(result.thread)


def test_todo_write_records_the_list_and_todos_reads_it() -> None:
    async def main() -> None:
        thread, events = await run([write(ITEMS), text("planned")])
        assert any(isinstance(e, TodosUpdatedEvent) for e in events)
        todos = await thread.todos()
        assert isinstance(todos, Ok)
        assert [t.model_dump(mode="json") for t in todos.value] == ITEMS

    asyncio.run(main())


def test_malformed_and_duplicate_lists_are_error_results_and_append_nothing() -> None:
    bad: JsonValue = [{"id": "1", "content": "x", "status": "someday"}]
    twice: JsonValue = [
        {"id": "1", "content": "a", "status": "pending"},
        {"id": "1", "content": "b", "status": "pending"},
    ]

    async def main() -> None:
        thread, events = await run([write(bad), write(twice, "call_2"), text("sorry")])
        assert not any(isinstance(e, TodosUpdatedEvent) for e in events)
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert [(r.data.is_error, r.data.origin) for r in results] == [
            (True, "not_executed"),
            (True, "not_executed"),
        ]
        assert "unique" in results[1].data.preview
        todos = await thread.todos()
        assert isinstance(todos, Ok)
        assert todos.value == ()

    asyncio.run(main())


def test_the_reminder_fires_once_after_ten_quiet_turns() -> None:
    async def main() -> None:
        quiet = REMIND_AFTER + 3
        model = scripted_model(
            {"responses": [write(ITEMS), text("planned"), *[text("ok")] * quiet]}
        )
        bot = agent(model=model)
        store = sqlite(":memory:")
        first = await bot.run("plan", store=store)
        assert isinstance(first, Completed)
        for n in range(quiet):
            done = await bot.run(f"status {n}?", store=store, thread=first.thread)
            assert isinstance(done, Completed)
        events = await events_of(first.thread)
        reminders = [e for e in events if isinstance(e, InjectedEvent) and e.data.source == "todo"]
        assert len(reminders) == 1
        assert reminders[0].data.text == "- [in_progress] Add the column\n- [pending] Backfill"
        assert reminders[0].data.trust == "untrusted_reference"
        at = events.index(reminders[0])
        assert events[at - 1].type == "user_input"
        assert events[at + 1].type == "model_request"

    asyncio.run(main())
