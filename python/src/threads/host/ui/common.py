"""What the UI routes share: the key's thread with its log, and the codes a start-and-stream
route answers before its stream starts."""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from threads.agents.store import now_ms, open_store
from threads.log import Event, ParkAddress, Principal, ThreadId
from threads.result import Err, Ok
from threads.thread.handle import Thread

if TYPE_CHECKING:
    from threads.host.app import Host

UI_CODES: Final = frozenset(
    {
        "unauthenticated",
        "forbidden",
        "invalid_request",
        "not_found",
        "branch_busy",
        "branch_not_runnable",
        "idempotency_key_reused",
        "approval_mismatch",
        "approval_expired",
        "approval_duplicate",
        "no_open_question",
    }
)


@dataclass(frozen=True, slots=True)
class UiLog:
    """The key's thread (with its approval authority), its main branch's events and parks."""

    thread: Thread
    events: Sequence[Event]
    parked: Sequence[ParkAddress]


async def ui_log(host: "Host", principal: Principal, thread_id: ThreadId) -> UiLog | None:
    """None when the key has no thread yet."""
    opened = await host.thread(principal, thread_id, None)
    if isinstance(opened, Err):
        return None
    thread = opened.value
    read = await (await open_store(thread.store)).read(thread.branch, now_ms())
    if not isinstance(read, Ok):
        return None
    return UiLog(thread, read.value.fold.events, read.value.fold.parked)
