"""One scheduler pass over a tenant: what every part of it shares, and how a part fails alone."""

import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from threads.agents.pinned import pinned_start
from threads.agents.store import Store
from threads.host.runs import Runner
from threads.store import Draft


@dataclass(frozen=True, slots=True)
class Pass:
    runner: Runner
    store: Store
    """The tenant's view of the host store."""
    tenant: str
    _pins: dict[str, Draft] = field(default_factory=dict[str, Draft])

    async def started(self, agent: str) -> Draft:
        """The thread_started the agent pins, set up once per pass however many schedules and
        threads use it."""
        found = self._pins.get(agent)
        if found is None:
            definition = self.runner.bound_to(agent).definition
            found = self._pins[agent] = await pinned_start(definition, self.store)
        return found


async def isolated(what: str, part: Callable[[], Awaitable[None]]) -> None:
    """Runs one part of a pass (a schedule's reservation, a thread's decisions) and reports its
    failure instead of raising, so one broken agent, row or thread never stops the others.
    Cancellation still propagates."""
    try:
        await part()
    except Exception as error:
        sys.stderr.write(f"threads host: {what} failed: {error}\n")
