"""The thread_started a new thread of an agent opens with, pinned outside any run: what a host
appends itself when it creates a thread (a schedule's), and what check() sets up to list."""

from contextlib import AsyncExitStack

from threads.agents.definition import Definition
from threads.agents.run import with_servers
from threads.agents.setup import set_up
from threads.agents.store import Store, open_store
from threads.loop.drafts import draft
from threads.store import Draft


async def pinned_start[D](definition: Definition[D], store: Store) -> Draft:
    """The agent's thread_started, its config durable first. Its tool servers are connected only
    to list their tools, which dispatches nothing, so no writer fences them. The agent is set up
    first, as a run would be: a setup failure raises ConfigError before anything is stored."""
    await set_up(definition)
    async with AsyncExitStack() as stack:
        started, config = (await with_servers(definition, stack, outside_any_branch)).pin()
    await (await open_store(store)).put_artifact(config)
    return draft("thread_started", started)


async def outside_any_branch() -> bool:
    """The fence for listing tools on no branch (check(), a host pinning a new thread): there is
    no lease to lose, and listing dispatches nothing."""
    return True
