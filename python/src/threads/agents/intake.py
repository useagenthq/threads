"""How a host run records its input.

`agent.run()` records a user_input and waits for the result. A host run differs in three ways:
the input may come with the event it was delivered by (a channel_delivery, a schedule_fired),
host rows are bound to the input's append (an idempotency receipt, an inbox item's consumption),
and the caller is told as soon as the input is durable, while the run goes on.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from threads.agents.bindings import ToolServer
from threads.loop.runtime import Halt, Runtime
from threads.store import Draft, StoredEvent
from threads.store.companion import Companion

if TYPE_CHECKING:
    import asyncio

type After = Callable[[Runtime], Awaitable[Halt | None]]
"""Host work derived from the log, under the run's lease (a channel's outbound replies): run
after recovery, before a new input, and again when the run idles or parks. None when it is
done; otherwise the halt it stopped at."""


@dataclass(frozen=True, slots=True)
class Intake:
    source: Literal["api", "channel", "schedule"]
    recorded: "asyncio.Future[StoredEvent]" = field(compare=False)
    """Resolved with the user_input once it is durable. Left pending when the run records no
    input (a busy or parked branch, a refused companion): the run's result says why."""
    before: tuple[Draft, ...] = ()
    """Appended with the user_input: the delivery it names."""
    delivery_event_id: str | None = None
    companion: Companion | None = None
    servers: tuple[ToolServer, ...] = ()
    """Host tool servers this run connects (a channel's send tool), pinned like MCP tools."""
    after: After | None = None
