"""Where a host run's stream goes: its appends wake the branch's subscribers and the thread's
live hub, and its text deltas go to the hub, keyed by the run's tenant."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from threadsai.agents.results import EventItem, StreamEvent
from threadsai.host.ui.live import Delta
from threadsai.log import EventId
from threadsai.thread.handle import Thread

if TYPE_CHECKING:
    from threadsai.host.runs import Runner


@dataclass(frozen=True, slots=True)
class Emit:
    runner: "Runner"
    thread: Thread
    tenant: str

    def __call__(self, item: StreamEvent) -> None:
        if isinstance(item, EventItem):
            self.runner.hub.appended(self.tenant, self.thread.id, item.event)
        self.runner.wake(self.thread.branch)

    def delta(self, request: EventId, part: int, text: str) -> None:
        self.runner.hub.delta(self.tenant, self.thread.id, Delta(request, part, text))
