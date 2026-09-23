"""Thread.usage, cost and cache_breaks (spec/api.json) through open_thread over real runs: each
is the projection of one verified read, and a corrupt log is log_corrupt, never zero."""

import asyncio
import sqlite3
from collections.abc import Callable, Coroutine, Sequence

import pytest
from pydantic import JsonValue, TypeAdapter

from threads import Agent, Completed, ConfigError, Store, agent, scripted_model, sqlite
from threads._generated.host_api_v1 import Cost
from threads.agents.store import now_ms, open_store
from threads.log import BranchId, OutputPart, ThreadId, ThreadStartedEvent, Usage
from threads.log import Model as ModelLimits
from threads.loop.drafts import draft
from threads.loop.model import Model, ModelInfo, ModelResponse
from threads.loop.scripted import ScriptedModel
from threads.reduce.projections import cost, merge_cost
from threads.reduce.state import UsageTotals, usage_totals
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.thread.handle import Thread, open_thread

PRICE: JsonValue = {"input": 3000, "output": 15_000}
ONE = 10 * 3000 + 2 * 15_000
"""One scripted response of 10 input and 2 output tokens at PRICE."""
USAGE: JsonValue = {"input_tokens": 10, "output_tokens": 2}
_PARTS: TypeAdapter[list[OutputPart]] = TypeAdapter(list[OutputPart])


class Priced(ScriptedModel):
    """A scripted model that declares a price; still scripted, so the request guard allows it."""

    @property
    def info(self) -> ModelInfo:
        base = super().info
        limits = {**base.limits.model_dump(mode="json"), "price": PRICE}
        return ModelInfo(
            base.model, base.adapter, base.params, ModelLimits.model_validate(limits), base.lookup
        )


def say(text: str, usage: JsonValue = USAGE) -> ModelResponse:
    parts = _PARTS.validate_python([{"type": "text", "text": text}])
    return ModelResponse(parts, "end_turn", Usage.model_validate(usage), None)


def spawn(name: str) -> ModelResponse:
    use = {"type": "tool_use", "call_id": f"s-{name}", "name": "spawn_agent"}
    parts = _PARTS.validate_python([{**use, "input": {"agent": name, "prompt": "Do it."}}])
    return ModelResponse(parts, "tool_use", Usage.model_validate(USAGE), None)


def usd(known: int, upper: int, *, complete: bool, bounded: bool) -> Cost:
    return Cost(
        currency="USD",
        known_nanos=known,
        upper_bound_nanos=upper,
        complete=complete,
        bounded=bounded,
    )


def priced(*replies: ModelResponse) -> Priced:
    return Priced(list(replies), {})


def free(text: str) -> ScriptedModel:
    reply: JsonValue = {
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    return scripted_model({"responses": [reply]})


async def run(store: Store, model: Model, subagents: Sequence[Agent[None]] = ()) -> Thread:
    result = await agent(name="lead", model=model, subagents=list(subagents)).run(
        "Go.", store=store
    )
    assert isinstance(result, Completed)
    return result.thread


async def corrupt(store: Store, thread: ThreadId, prompt: str) -> None:
    """Rewrites one stored line of `thread`'s main branch so its chain no longer verifies."""
    sq = await open_store(store)
    root = await sq.root(thread)
    assert isinstance(root, Ok)
    sql = (
        "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), ?, 'Edited.') AS BLOB)"
        " WHERE branch_id = ? AND seq = 2"
    )

    def edit(c: sqlite3.Connection) -> None:
        c.execute(sql, (prompt, root.value))

    await sq.run(edit)


async def only_child(thread: Thread) -> ThreadId:
    children = await thread.children()
    assert isinstance(children, Ok)
    (child,) = children.value
    return child.child_thread_id


def check(body: Callable[[Store], Coroutine[None, None, None]]) -> None:
    asyncio.run(body(sqlite(":memory:")))


def test_usage_is_the_reduced_usage_and_unknown_is_never_zero() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        assert await thread.usage() == Ok(usage_totals(read.value.fold))
        assert await thread.usage() == Ok(UsageTotals(10, 2, 0))
        unknown = say("Hi.", {"input_tokens": None, "output_tokens": 2})
        other = await run(store, priced(unknown))
        assert await other.usage() == Ok(UsageTotals(0, 2, 1))

    check(body)


def test_a_priced_agent_pins_usd_and_costs_the_projection() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        own = await thread.cost()
        assert own == Ok(cost(read.value.fold))
        assert own == Ok(usd(ONE, ONE, complete=True, bounded=True))

    check(body)


def test_an_unpriced_model_pins_no_currency_so_cost_is_none() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, free("Hi."))
        assert await thread.cost() == Ok(None)
        assert await thread.cost(tree=True) == Ok(None)

    check(body)


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


def test_cache_breaks_uses_the_default_ttl_when_no_context_is_pinned() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        assert read.value.fold.started is not None
        assert "context" not in read.value.fold.started.model_dump(mode="json")["policy"]
        assert await thread.cache_breaks() == Ok(())

    check(body)


def test_a_log_with_only_thread_started() -> None:
    async def body(store: Store) -> None:
        sq = await open_store(store)
        thread_id, branch = ThreadId(uuid7(now_ms())), uuid7(now_ms())
        started: dict[str, JsonValue] = {
            "agent_name": "test",
            "config_hash": "0" * 64,
            "instructions": "Test.",
            "model": {"provider": "scripted", "name": "scripted-1"},
            "model_params": {"max_tokens": 1024},
            "adapter": {"name": "scripted", "version": "1", "settings": {}},
            "tools": [],
        }
        root = BranchId(branch)
        assert await sq.create(thread_id, root, now_ms()) == Ok(None)
        writer = await sq.acquire(root, "holder", now_ms)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([draft("thread_started", started)]), Ok)
        opened = await open_thread(store, thread_id)
        assert isinstance(opened, Ok)
        assert await opened.value.usage() == Ok(UsageTotals(0, 0, 0))
        assert await opened.value.cost() == Ok(None)
        assert await opened.value.cache_breaks() == Ok(())

    check(body)


def test_a_corrupt_log_is_log_corrupt_from_every_method() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        await corrupt(store, thread.id, "Go.")
        for result in (
            await thread.usage(),
            await thread.cost(),
            await thread.cost(tree=True),
            await thread.cache_breaks(),
        ):
            assert isinstance(result, Err)
            assert result.error.code == "log_corrupt"

    check(body)


def test_a_line_from_a_newer_writer_is_unsupported_not_corrupt() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        sql = (
            "UPDATE events SET line = CAST(replace(CAST(line AS TEXT),"
            ' \'"type":"turn_completed"\', \'"type":"approval_quorum"\') AS BLOB)'
            " WHERE branch_id = ?"
        )

        def edit(c: sqlite3.Connection) -> None:
            c.execute(sql, (thread.branch,))

        await (await open_store(store)).run(edit)
        for result in (
            await thread.usage(),
            await thread.cost(tree=True),
            await thread.cache_breaks(),
        ):
            assert isinstance(result, Err)
            assert result.error.code == "unsupported_critical_event"

    check(body)


def test_a_priced_thread_pinned_before_currency_reads_but_refuses_to_continue() -> None:
    async def body(store: Store) -> None:
        # What an older agent() stored: the same pin without policy.currency, so another hash.
        fresh = await run(sqlite(":memory:"), priced(say("Hi.")))
        timeline = await fresh.timeline()
        assert isinstance(timeline, Ok)
        first = timeline.value.entries[0].event
        assert isinstance(first, ThreadStartedEvent)
        data = first.data.model_dump(mode="json", by_alias=True)
        policy = {k: v for k, v in data["policy"].items() if k != "currency"}
        old_pin: dict[str, JsonValue] = {**data, "policy": policy, "config_hash": "0" * 64}
        sq = await open_store(store)
        thread_id, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
        assert await sq.create(thread_id, branch, now_ms()) == Ok(None)
        writer = await sq.acquire(branch, "older", now_ms)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([draft("thread_started", old_pin)]), Ok)
        await writer.value.release()
        old = await open_thread(store, thread_id)
        assert isinstance(old, Ok)
        assert await old.value.cost() == Ok(None)
        again = agent(name="lead", model=priced(say("More.")))
        with pytest.raises(ConfigError, match="another config"):
            await again.run("More.", thread=old.value)

    check(body)
