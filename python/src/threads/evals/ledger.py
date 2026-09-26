"""What a simulated conversation has spent so far (spec lane 32, B.5). A continued prefix is
already on the thread, so the ledger starts at the imported log's seq and cost: only what this
conversation appends is charged to its budget. One monotonic clock covers both threads."""

import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

from threads.agents.results import Thread
from threads.evals.remaining_budget import Spent
from threads.evals.tree_totals import Totals, tree_totals
from threads.result import Ok


def monotonic_ms() -> int:
    return time.monotonic_ns() // 1_000_000


@dataclass(frozen=True, slots=True)
class Ledger:
    since: int
    """Events at or below this seq on the agent thread are the imported prefix."""
    base_cost_nanos: int
    started_ms: int
    clock: Callable[[], int] = field(default=monotonic_ms, compare=False, repr=False)


def start_ledger(
    since: int, base_cost_nanos: int, clock: Callable[[], int] = monotonic_ms
) -> Ledger:
    return Ledger(since, base_cost_nanos, clock(), clock)


async def cost_bound(thread: Thread) -> int:
    """The upper bound of a thread's cost, the bound unknown usage is charged at."""
    got = await thread.cost(tree=True)
    if not isinstance(got, Ok) or got.value is None:
        return 0
    return got.value.upper_bound_nanos


async def _bound(threads: Sequence[Thread]) -> int:
    total = 0
    for t in threads:
        total += await cost_bound(t)
    return total


async def spent_so_far(led: Ledger, agent: Thread | None, user: Thread | None) -> Spent:
    """Both threads' totals since the ledger started, as the budget arithmetic reads them."""
    a = Totals() if agent is None else await tree_totals(agent.store, agent.branch, led.since)
    u = Totals() if user is None else await tree_totals(user.store, user.branch)
    threads = [t for t in (agent, user) if t is not None]
    cost: int = max(0, await _bound(threads) - led.base_cost_nanos)
    return Spent(
        requests=a.requests + u.requests,
        input_tokens=a.input_tokens + u.input_tokens,
        output_tokens=a.output_tokens + u.output_tokens,
        turns=a.turns + u.turns,
        cost_nanos=cost,
        wall_ms=led.clock() - led.started_ms,
    )
