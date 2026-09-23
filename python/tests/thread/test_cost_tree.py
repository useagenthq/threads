"""Thread.cost(tree=True): every descendant at any depth, each read from its own log."""

import sqlite3

from thread.usage_kit import (
    ONE,
    ScriptedModel,
    check,
    corrupt,
    free,
    only_child,
    priced,
    run,
    say,
    spawn,
    unpriced_middle,
    usd,
)

from threads import Store, agent
from threads.agents.store import now_ms, open_store
from threads.reduce.projections import merge_cost
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.thread.handle import open_thread


def test_tree_adds_the_child_and_grandchild_from_their_own_logs() -> None:
    async def body(store: Store) -> None:
        leaf = agent(name="leaf", model=priced(say("Leaf.")))
        mid = agent(name="mid", model=priced(spawn("leaf"), say("Mid.")), subagents=[leaf])
        thread = await run(store, priced(spawn("mid"), say("Done.")), [mid])
        assert await thread.cost() == Ok(usd(2 * ONE, 2 * ONE, complete=True, bounded=True))
        # Lead 2 responses, mid 2, leaf 1.
        total = await thread.cost(tree=True)
        assert total == Ok(usd(5 * ONE, 5 * ONE, complete=True, bounded=True))

    check(body)


def test_an_unpriced_child_adds_nothing_and_makes_the_total_incomplete() -> None:
    async def body(store: Store) -> None:
        child = agent(name="free", model=free("Free."))
        thread = await run(store, priced(spawn("free"), say("Done.")), [child])
        total = await thread.cost(tree=True)
        assert total == Ok(usd(2 * ONE, 2 * ONE, complete=False, bounded=False))

    check(body)


def test_an_unpriced_root_is_none_even_over_priced_children() -> None:
    async def body(store: Store) -> None:
        child = agent(name="kid", model=priced(say("Kid.")))
        unpriced = ScriptedModel([spawn("kid"), say("Done.")], {})
        thread = await run(store, unpriced, [child])
        assert await thread.cost(tree=True) == Ok(None)

    check(body)


def test_a_part_in_another_currency_adds_nothing_and_makes_the_total_incomplete() -> None:
    total = usd(5, 7, complete=True, bounded=True)
    eur = total.model_copy(update={"currency": "EUR"})
    assert merge_cost(total, eur) == usd(5, 7, complete=False, bounded=False)
    assert merge_cost(total, usd(5, 7, complete=False, bounded=True)) == usd(
        10, 14, complete=False, bounded=True
    )


def test_a_spawned_child_with_no_thread_was_never_started() -> None:
    async def body(store: Store) -> None:
        child = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, priced(spawn("kid"), say("Done.")), [child])
        kid = await only_child(thread)
        elsewhere = uuid7(now_ms())

        # As if the parent crashed between agent_spawned and the child's first line.
        def move(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO threads (thread_id, tenant_id)"
                " SELECT ?, tenant_id FROM threads WHERE thread_id = ?",
                (elsewhere, kid),
            )
            c.execute("UPDATE branches SET thread_id = ? WHERE thread_id = ?", (elsewhere, kid))

        await (await open_store(store)).run(move)
        total = await thread.cost(tree=True)
        assert total == Ok(usd(2 * ONE, 2 * ONE, complete=True, bounded=True))

    check(body)


def test_a_corrupt_child_fails_the_tree_with_log_corrupt() -> None:
    async def body(store: Store) -> None:
        child = agent(name="kid", model=priced(say("Kid.")))
        thread = await run(store, priced(spawn("kid"), say("Done.")), [child])
        kid = await only_child(thread)
        await corrupt(store, kid, "Do it.")
        total = await thread.cost(tree=True)
        assert isinstance(total, Err)
        assert total.error.code == "log_corrupt"
        assert kid in total.error.message
        assert await thread.cost() == Ok(usd(2 * ONE, 2 * ONE, complete=True, bounded=True))

    check(body)


def test_an_unpriced_child_does_not_stop_the_walk() -> None:
    async def body(store: Store) -> None:
        thread = await unpriced_middle(store)
        # Lead 2 responses and leaf 1; mid adds nothing but its lack of a price.
        total = await thread.cost(tree=True)
        assert total == Ok(usd(3 * ONE, 3 * ONE, complete=False, bounded=False))

    check(body)


def test_a_corrupt_grandchild_under_an_unpriced_child_fails_the_tree() -> None:
    async def body(store: Store) -> None:
        thread = await unpriced_middle(store)
        mid = await only_child(thread)
        opened = await open_thread(store, mid)
        assert isinstance(opened, Ok)
        leaf = await only_child(opened.value)
        await corrupt(store, leaf, "Do it.")
        total = await thread.cost(tree=True)
        assert isinstance(total, Err)
        assert total.error.code == "log_corrupt"
        assert f"child {mid}: child {leaf}:" in total.error.message

    check(body)
