"""The pending_wakes fold (spec/schema/store.sql; Gate 1 §2.7.3): the background children of a
branch still waiting to report. The store's rows (threads.store.wakes) follow it, and the
conformance `pending_wakes` projection reads it."""

from collections.abc import Sequence

from pydantic import JsonValue

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    BranchId,
    Event,
    ParkedEvent,
    UnknownEvent,
)
from threads.reduce.fold import Fold


def change(event: Event | UnknownEvent) -> tuple[bool, str] | None:
    """The row an event inserts (True) or deletes (False), if any."""
    if isinstance(event, AgentSpawnedEvent) and event.data.mode == "background":
        return True, event.data.child_thread_id
    if isinstance(event, AgentFinishedEvent):
        return False, event.data.child_thread_id
    if isinstance(event, ParkedEvent) and event.data.address.kind == "child":
        return False, event.data.address.id
    return None


def pending_wakes(events: Sequence[Event], branch: BranchId) -> tuple[str, ...]:
    """The background children of `branch` still waiting to report, in spawn order."""
    rows: dict[str, None] = {}
    for event in events:
        found = change(event) if event.branch_id == branch else None
        if found is None:
            continue
        added, child = found
        if added:
            rows[child] = None
        else:
            rows.pop(child, None)
    return tuple(rows)


def projection(fold: Fold) -> JsonValue:
    """The rows the branch's own events rebuild, sorted."""
    branch = fold.segment
    if branch is None:
        return []
    rows = sorted(pending_wakes(fold.events, branch))
    return [{"branch_id": branch, "child_thread_id": child} for child in rows]
