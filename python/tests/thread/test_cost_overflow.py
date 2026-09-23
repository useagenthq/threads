"""Cost nanos are wire integers (at most 2^53 - 1): past that, cost() is cost_overflow, a Result
error, never a saturated amount or a raised ValidationError (spec/api.json Thread.cost)."""

from pydantic import JsonValue
from thread.usage_kit import check, child_ids, priced, run, say, spawn, unpriced

from threads import Store, agent
from threads.result import Err, Ok
from threads.thread.handle import open_thread

HUGE: JsonValue = {"input": 600_000_000_000_000, "output": 0}
"""One scripted response (10 input tokens) at this price costs 6e15 nanos: within the range."""
ONE_HUGE = 10 * 600_000_000_000_000


def test_a_thread_whose_own_cost_passes_the_range() -> None:
    async def body(store: Store) -> None:
        free = agent(name="free", model=unpriced(say("Free.")))
        # Two responses at HUGE: 1.2e16 nanos.
        thread = await run(store, priced(spawn("free"), say("Done."), price=HUGE), [free])
        for result in (await thread.cost(), await thread.cost(tree=True)):
            assert isinstance(result, Err)
            assert result.error.code == "cost_overflow"

    check(body)


def test_a_tree_total_past_the_range_though_every_thread_fits() -> None:
    async def body(store: Store) -> None:
        one = agent(name="one", model=priced(say("One."), price=HUGE))
        two = agent(name="two", model=priced(say("Two."), price=HUGE))
        thread = await run(store, priced(spawn("one"), spawn("two"), say("Done.")), [one, two])
        assert isinstance(await thread.cost(), Ok)
        child = await open_thread(store, (await child_ids(thread))[0])
        assert isinstance(child, Ok)
        own = await child.value.cost()
        assert isinstance(own, Ok)
        assert own.value is not None
        assert own.value.known_nanos == ONE_HUGE
        total = await thread.cost(tree=True)
        assert isinstance(total, Err)
        assert total.error.code == "cost_overflow"

    check(body)
