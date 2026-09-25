"""The budget ledger: concurrent descendants reserving at once never pass a
limit (the property), a refusal inserts nothing, and settlement replaces a
reservation with its disposition."""

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from threads.result import Ok
from threads.store import SqliteStore
from threads.store.budgets import Cover, Refused


async def _store() -> SqliteStore:
    opened = await SqliteStore.open()
    assert isinstance(opened, Ok)
    return opened.value


@settings(max_examples=40, deadline=None)
@given(
    limit=st.integers(min_value=0, max_value=500),
    amounts=st.lists(st.integers(min_value=0, max_value=120), min_size=1, max_size=24),
)
def test_concurrent_reservations_never_exceed_a_limit(limit: int, amounts: list[int]) -> None:
    async def main() -> None:
        store = await _store()
        ledger = store.budgets
        root = Cover("run:root:input", {"max_cost_nanos": limit})
        # Each descendant is covered by the shared root budget and its own thread budget.
        results = await asyncio.gather(
            *(
                ledger.reserve(
                    f"branch{n}:1",
                    [Cover(f"thread:child{n}", {"max_cost_nanos": 10_000}), root],
                    {"max_cost_nanos": amount},
                )
                for n, amount in enumerate(amounts)
            )
        )
        accepted = [a for a, r in zip(amounts, results, strict=True) if r is None]
        assert sum(accepted) <= limit
        for amount, refused in zip(amounts, results, strict=True):
            if refused is not None:
                assert refused.budget_id == root.budget_id
                assert refused.observed > limit
                assert refused.observed - amount <= limit
        await store.close()

    asyncio.run(main())


def test_a_refusal_inserts_nothing_and_settle_replaces_the_bound() -> None:
    async def main() -> None:
        store = await _store()
        ledger = store.budgets
        covers = [Cover("thread:t", {"max_cost_nanos": 100, "max_model_requests": 2})]
        assert (
            await ledger.reserve("b:1", covers, {"max_cost_nanos": 80, "max_model_requests": 1})
            is None
        )
        refused = await ledger.reserve(
            "b:2", covers, {"max_cost_nanos": 30, "max_model_requests": 1}
        )
        assert refused == Refused("thread:t", "max_cost_nanos", 100, 110)
        assert await ledger.attempts("b") == {"b:1": True}
        await ledger.settle("b:1", {"max_cost_nanos": 10, "max_model_requests": 1})
        assert await ledger.attempts("b") == {"b:1": False}
        assert (
            await ledger.reserve("b:2", covers, {"max_cost_nanos": 30, "max_model_requests": 1})
            is None
        )
        third = await ledger.reserve("b:3", covers, {"max_cost_nanos": 0, "max_model_requests": 1})
        assert third == Refused("thread:t", "max_model_requests", 2, 3)
        await ledger.release("b:2")
        assert await ledger.attempts("b") == {"b:1": False}
        await store.close()

    asyncio.run(main())


def test_reserving_the_same_key_again_rechecks_and_replaces_it_as_typescript_does() -> None:
    """A commit whose outcome was unknown is retried under its key: the retry is checked against
    everything but its own rows, then upserts its amount (TS BudgetLedger.reserve)."""

    async def main() -> None:
        store = await _store()
        ledger = store.budgets
        covers = [Cover("thread:t", {"max_cost_nanos": 100})]
        larger = 90
        assert await ledger.reserve("b:1", covers, {"max_cost_nanos": 60}) is None
        # Again, larger: 90 fits once its own 60 is left out, and replaces it.
        assert await ledger.reserve("b:1", covers, {"max_cost_nanos": larger}) is None
        assert await ledger.spent("thread:t", "max_cost_nanos") == larger
        # Again, past the limit: refused, and the earlier reservation stays.
        refused = await ledger.reserve("b:1", covers, {"max_cost_nanos": 101})
        assert refused == Refused("thread:t", "max_cost_nanos", 100, 101)
        assert await ledger.spent("thread:t", "max_cost_nanos") == larger
        # After settlement, reserving the key again makes it reserved once more.
        await ledger.settle("b:1", {"max_cost_nanos": 10})
        assert await ledger.reserve("b:1", covers, {"max_cost_nanos": 20}) is None
        assert await ledger.attempts("b") == {"b:1": True}
        await store.close()

    asyncio.run(main())
