"""What a thread pinned at its start, and which agent of a host binding answers for a name:
how the host decides the definition a thread's run is bound to (runs.py)."""

from threadsai.agents.definition import Definition
from threadsai.log import ThreadId, ThreadStartedEvent
from threadsai.result import Ok
from threadsai.store import SqliteStore


async def pinned_agent(sq: SqliteStore, thread_id: ThreadId) -> tuple[str, bool] | None:
    """The agent a thread pinned at its start, and whether it pinned ask_user; None before it
    started."""
    root = await sq.root(thread_id)
    read = None if not isinstance(root, Ok) else await sq.read(root.value, 0)
    if read is None or not isinstance(read, Ok):
        return None
    events = read.value.fold.events
    started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
    if started is None:
        return None
    return started.data.agent_name, any(t.name == "ask_user" for t in started.data.tools)


def reachable(definition: Definition[None], name: str) -> Definition[None] | None:
    """The agent, or a handoff target reachable from it, with this name."""
    if definition.name == name:
        return definition
    return next((d for h in definition.handoffs if (d := reachable(h, name)) is not None), None)
