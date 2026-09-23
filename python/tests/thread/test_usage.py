"""Thread.usage, cost and cache_breaks (spec/api.json) through open_thread over real runs: each
is the projection of one verified read, and a corrupt log is log_corrupt, never zero."""

import json
import sqlite3

import pytest
from pydantic import JsonValue
from thread.rewrite_log import rewrite_log
from thread.usage_kit import ONE, check, corrupt, priced, run, say, unpriced, usd

from threads import ConfigError, Store, agent
from threads.agents.store import now_ms, open_store
from threads.log import BranchId, ThreadId, UsageTotals
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.loop.drafts import draft
from threads.reduce.projections import cost
from threads.reduce.state import usage_totals
from threads.result import Err, Ok
from threads.store.lines import uuid7
from threads.thread.handle import open_thread


def totals(input_tokens: int, output_tokens: int, unknown_responses: int) -> UsageTotals:
    return UsageTotals(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        unknown_responses=unknown_responses,
    )


def test_usage_is_the_reduced_usage_and_unknown_is_never_zero() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        assert await thread.usage() == Ok(usage_totals(read.value.fold))
        assert await thread.usage() == Ok(totals(10, 2, 0))
        unknown = say("Hi.", {"input_tokens": None, "output_tokens": 2})
        other = await run(store, priced(unknown))
        assert await other.usage() == Ok(totals(0, 2, 1))

    check(body)


def test_a_priced_agent_pins_usd_and_costs_the_projection() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))
        read = await (await open_store(store)).read(thread.branch, now_ms())
        assert isinstance(read, Ok)
        own = await thread.cost()
        assert own == cost(read.value.fold)
        assert own == Ok(usd(ONE, exact=True))

    check(body)


def test_an_unpriced_model_pins_no_currency_so_cost_is_none() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, unpriced(say("Hi.")))
        assert await thread.cost() == Ok(None)
        assert await thread.cost(tree=True) == Ok(None)

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
        ran = await run(store, unpriced(say("Hi.")))
        # A real pin, as if the host crashed right after thread_started.
        await rewrite_log(store, ran.id, lambda lines: lines[:1])
        opened = await open_thread(store, ran.id)
        assert isinstance(opened, Ok)
        assert await opened.value.usage() == Ok(totals(0, 0, 0))
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
            await thread.timeline(),
        ):
            assert isinstance(result, Err)
            assert result.error.code == "unsupported_critical_event"

    check(body)


def legacy_pin() -> dict[str, JsonValue]:
    """What agent() stored for a priced model before the currency rule: its pin without
    policy.currency, hashed the same way."""
    data, raw = agent(name="lead", model=priced(say("Hi."))).definition.pin()
    # The stored hash is the hash of these bytes, so dropping currency from them is the old pin.
    assert sha256_hex(raw) == data["config_hash"]
    config = json.loads(raw)
    del config["policy"]["currency"]
    text = canonicalize(config)
    assert isinstance(text, Ok)
    return {**data, "policy": config["policy"], "config_hash": sha256_hex(text.value.encode())}


def test_a_priced_thread_pinned_before_currency_reads_but_refuses_to_continue() -> None:
    async def body(store: Store) -> None:
        sq = await open_store(store)
        thread_id, branch = ThreadId(uuid7(now_ms())), BranchId(uuid7(now_ms()))
        assert await sq.create(thread_id, branch, now_ms()) == Ok(None)
        writer = await sq.acquire(branch, "older", now_ms)
        assert isinstance(writer, Ok)
        assert isinstance(await writer.value.append([draft("thread_started", legacy_pin())]), Ok)
        await writer.value.release()
        old = await open_thread(store, thread_id)
        assert isinstance(old, Ok)
        assert await old.value.cost() == Ok(None)
        again = agent(name="lead", model=priced(say("More.")))
        with pytest.raises(ConfigError, match="another config"):
            await again.run("More.", thread=old.value)

    check(body)


def test_a_branch_gone_after_open_is_log_corrupt_the_declared_error() -> None:
    async def body(store: Store) -> None:
        thread = await run(store, priced(say("Hi.")))

        # Another tenant's now: to this store the branch no longer exists.
        def move(c: sqlite3.Connection) -> None:
            c.execute("PRAGMA foreign_keys = OFF")
            c.execute(
                "UPDATE branches SET tenant_id = 'elsewhere' WHERE branch_id = ?", (thread.branch,)
            )

        await (await open_store(store)).run(move)
        for result in (await thread.usage(), await thread.cost(), await thread.cache_breaks()):
            assert isinstance(result, Err)
            assert result.error.code == "log_corrupt"

    check(body)
