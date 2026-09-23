"""Thread.cost(tree=True) over real child logs: the tree merge of spec/api.json Thread.cost, and
the tree's integrity (each child names the spawn that started it; no thread twice)."""

import json
import sqlite3

import pytest
from pydantic import JsonValue, ValidationError
from thread.rewrite_log import Line, edit_started, obj, rewrite_log
from thread.usage_kit import (
    ONE,
    check,
    child_ids,
    corrupt,
    priced,
    run,
    say,
    spawn,
    unpriced,
    unpriced_middle,
    usd,
)

from threads import Store, agent
from threads.agents.store import now_ms, open_store
from threads.log import AgentSpawnedEvent, Cost, ThreadId
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.thread.handle import Thread, open_thread


async def handle(store: Store, thread: ThreadId) -> Thread:
    opened = await open_thread(store, thread)
    assert isinstance(opened, Ok)
    return opened.value


async def first_spawn(thread: Thread) -> dict[str, JsonValue]:
    """The first agent_spawned of `thread`: what its child's thread_started must name."""
    timeline = await thread.timeline()
    assert isinstance(timeline, Ok)
    spawn_event = next(
        e.event for e in timeline.value.entries if isinstance(e.event, AgentSpawnedEvent)
    )
    return {
        "thread_id": spawn_event.thread_id,
        "branch_id": spawn_event.branch_id,
        "event_id": spawn_event.event_id,
        "relation": "subagent",
    }


async def failure(thread: Thread) -> tuple[str, str]:
    total = await thread.cost(tree=True)
    assert isinstance(total, Err)
    return total.error.code, total.error.message


def test_the_tree_adds_the_child_and_grandchild_from_their_own_logs() -> None:
    async def body(store: Store) -> None:
        leaf = agent(name="leaf", model=priced(say("Leaf.")))
        mid = agent(name="mid", model=priced(spawn("leaf"), say("Mid.")), subagents=[leaf])
        thread = await run(store, priced(spawn("mid"), say("Done.")), [mid])
        assert await thread.cost() == Ok(usd(2 * ONE, exact=True))
        # Lead 2 responses, mid 2, leaf 1.
        assert await thread.cost(tree=True) == Ok(usd(5 * ONE, exact=True))

    check(body)


def test_an_unpriced_root_counts_in_its_first_priced_descendants_currency() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, unpriced(spawn("kid"), say("Done.")), [kid])
        assert await thread.cost() == Ok(None)
        assert await thread.cost(tree=True) == Ok(usd(ONE, exact=False))

    check(body)


def test_an_all_unpriced_tree_is_none() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="free", model=unpriced(say("Free.")))
        thread = await run(store, unpriced(spawn("free"), say("Done.")), [kid])
        assert await thread.cost(tree=True) == Ok(None)

    check(body)


def test_mixed_siblings_an_unpriced_child_that_ran_makes_the_total_incomplete() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=priced(say("Kid.")))
        free = agent(name="free", model=unpriced(say("Free.")))
        lead = priced(spawn("kid"), spawn("free"), say("Done."))
        thread = await run(store, lead, [kid, free])
        assert await thread.cost(tree=True) == Ok(usd(4 * ONE, exact=False))

    check(body)


def test_several_unpriced_levels_do_not_stop_the_walk() -> None:
    async def body(store: Store) -> None:
        leaf = agent(name="leaf", model=priced(say("Leaf.")))
        low = agent(name="low", model=unpriced(spawn("leaf"), say("Low.")), subagents=[leaf])
        high = agent(name="high", model=unpriced(spawn("low"), say("High.")), subagents=[low])
        thread = await run(store, priced(spawn("high"), say("Done.")), [high])
        # Lead 2 responses and leaf 1; the unpriced levels add nothing but their lack of a price.
        assert await thread.cost(tree=True) == Ok(usd(3 * ONE, exact=False))

    check(body)


def test_a_child_in_another_currency_adds_nothing_and_makes_the_total_incomplete() -> None:
    async def body(store: Store) -> None:
        eur = agent(name="eur", model=priced(say("Euro.")))
        kid = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, priced(spawn("eur"), spawn("kid"), say("Done.")), [eur, kid])
        eur_id = (await child_ids(thread))[0]

        def to_eur(data: Line) -> Line:
            return {**data, "policy": {**obj(data["policy"]), "currency": "EUR"}}

        await rewrite_log(store, eur_id, edit_started(to_eur))
        own = await (await handle(store, eur_id)).cost()
        assert isinstance(own, Ok)
        assert own.value is not None
        assert own.value.currency == "EUR"
        assert await thread.cost(tree=True) == Ok(usd(4 * ONE, exact=False))

    check(body)


def test_a_started_child_that_made_no_model_request_changes_nothing() -> None:
    async def body(store: Store) -> None:
        free = agent(name="free", model=unpriced(say("Free.")))
        thread = await run(store, priced(spawn("free"), say("Done.")), [free])
        assert await thread.cost(tree=True) == Ok(usd(2 * ONE, exact=False))
        # As if it crashed after its input, before its first request: thread_started, user_input.
        await rewrite_log(store, (await child_ids(thread))[0], lambda lines: lines[:2])
        assert await thread.cost(tree=True) == Ok(usd(2 * ONE, exact=True))

    check(body)


def test_a_spawned_child_with_no_thread_was_never_started() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=unpriced(say("Kid.")))
        thread = await run(store, priced(spawn("kid"), say("Done.")), [kid])
        kid_id = (await child_ids(thread))[0]
        elsewhere = uuid7(now_ms())

        # As if the parent crashed between agent_spawned and the child's first line.
        def move(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO threads (thread_id, tenant_id)"
                " SELECT ?, tenant_id FROM threads WHERE thread_id = ?",
                (elsewhere, kid_id),
            )
            c.execute("UPDATE branches SET thread_id = ? WHERE thread_id = ?", (elsewhere, kid_id))

        await (await open_store(store)).run(move)
        assert await thread.cost(tree=True) == Ok(usd(2 * ONE, exact=True))

    check(body)


def test_a_corrupt_child_is_log_corrupt_naming_it() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, priced(spawn("kid"), say("Done.")), [kid])
        kid_id = (await child_ids(thread))[0]
        await corrupt(store, kid_id, "Do it.")
        code, message = await failure(thread)
        assert code == "log_corrupt"
        assert f"child {kid_id}:" in message
        assert await thread.cost() == Ok(usd(2 * ONE, exact=True))

    check(body)


def test_a_corrupt_grandchild_under_an_unpriced_child_is_log_corrupt_naming_the_path() -> None:
    async def body(store: Store) -> None:
        thread = await unpriced_middle(store)
        mid = (await child_ids(thread))[0]
        leaf = (await child_ids(await handle(store, mid)))[0]
        await corrupt(store, leaf, "Do it.")
        code, message = await failure(thread)
        assert code == "log_corrupt"
        assert f"child {mid}: child {leaf}:" in message

    check(body)


def test_a_descendant_in_a_newer_format_is_unsupported_format() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, priced(spawn("kid"), say("Done.")), [kid])
        kid_id = (await child_ids(thread))[0]
        sql = (
            "UPDATE branches SET header_line ="
            " CAST(replace(CAST(header_line AS TEXT), ?, ?) AS BLOB) WHERE thread_id = ?"
        )
        versions = ('"format_version":1', '"format_version":2')

        def newer(c: sqlite3.Connection) -> None:
            c.execute(sql, (*versions, kid_id))

        await (await open_store(store)).run(newer)
        code, message = await failure(thread)
        assert code == "unsupported_format"
        assert f"child {kid_id}:" in message

    check(body)


def test_a_child_whose_thread_started_names_another_parent_is_log_corrupt() -> None:
    async def body(store: Store) -> None:
        kid = agent(name="kid", model=priced(say("Kid.")))
        one = await run(store, priced(spawn("kid"), say("Done.")), [kid])
        other = agent(name="kid", model=priced(say("Kid.")))
        two = await run(store, priced(spawn("kid"), say("Done.")), [other])
        # A cross-linked import: one's child claims two's spawn.
        claimed = await first_spawn(two)
        kid_id = (await child_ids(one))[0]
        await rewrite_log(store, kid_id, edit_started(lambda d: {**d, "parent": claimed}))
        code, message = await failure(one)
        assert code == "log_corrupt"
        assert "doesn't name the agent_spawned" in message

    check(body)


def test_a_cycle_is_log_corrupt_never_an_endless_walk() -> None:
    async def body(store: Store) -> None:
        leaf = agent(name="leaf", model=priced(say("Leaf.")))
        mid = agent(name="mid", model=priced(spawn("leaf"), say("Mid.")), subagents=[leaf])
        lead = await run(store, priced(spawn("mid"), say("Done.")), [mid])
        middle = await handle(store, (await child_ids(lead))[0])
        leaf_id = (await child_ids(middle))[0]

        # mid's log now spawns lead, and lead names that spawn as its parent: lead → mid → lead.
        def spawn_lead(lines: list[Line]) -> list[Line]:
            return [obj(json.loads(json.dumps(line).replace(leaf_id, lead.id))) for line in lines]

        await rewrite_log(store, middle.id, spawn_lead)
        spawn_of_lead = await first_spawn(middle)
        await rewrite_log(store, lead.id, edit_started(lambda d: {**d, "parent": spawn_of_lead}))
        code, message = await failure(lead)
        assert code == "log_corrupt"
        assert f"thread {lead.id} appears twice" in message

    check(body)


def test_cost_rejects_a_lowercase_currency_and_a_fractional_amount() -> None:
    valid = usd(ONE, exact=True).model_dump()
    assert Cost.model_validate(valid) == usd(ONE, exact=True)
    with pytest.raises(ValidationError):
        Cost.model_validate({**valid, "currency": "usd"})
    with pytest.raises(ValidationError):
        Cost.model_validate({**valid, "known_nanos": 1.5})
