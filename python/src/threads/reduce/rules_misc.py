"""Compaction, snapshots, forks, children, todos, teams and branch-unique keys (semantic rules 9,
10, 22, 23, 24 in spec/schema/README.md)."""

from collections.abc import Mapping

from pydantic.experimental.missing_sentinel import MISSING

from threads.log import (
    AgentFinishedEvent,
    AgentSpawnedEvent,
    ChannelDeliveryEvent,
    CompactedEvent,
    ForkEvent,
    ParseError,
    ScheduleFiredEvent,
    ScheduleSkippedEvent,
    SnapshotEvent,
    TeamMessageEvent,
    TeamTaskClaimedEvent,
    TeamTaskCreatedEvent,
    TeamTaskUpdatedEvent,
    TextPart,
    TodosUpdatedEvent,
)
from threads.log.digest import sha256_hex
from threads.reduce import rules_requested
from threads.reduce.fold import Fold, Task, reject
from threads.reduce.handlers import Handler, on


def _compacted(fold: Fold, event: CompactedEvent) -> ParseError | None:
    start, end = event.data.from_seq, event.data.to_seq
    if not start <= end < event.seq:
        return reject(event, "a compacted range must lie before the compacted event")
    if start - 1 not in fold.boundaries or end not in fold.boundaries:
        return reject(event, "a compacted range must start and end at step boundaries")
    for other_start, other_end in fold.ranges:
        overlaps = start <= other_end and other_start <= end
        nested = (start <= other_start and other_end <= end) or (
            other_start <= start and end <= other_end
        )
        if overlaps and not nested:
            return reject(event, "compacted ranges may nest but never partly overlap")
    error = _summary_error(fold, event)
    if error is not None:
        return reject(event, error)
    requested = rules_requested.compacted_error(fold, event)
    if requested is not None:
        return requested
    fold.ranges.append((start, end))
    rules_requested.answer(fold, event.data.cause_event_id)
    return None


def _summary_error(fold: Fold, event: CompactedEvent) -> str | None:
    request = event.data.summary_request_event_id
    if request is MISSING:
        return None
    response = fold.responses.get(request)
    if request not in fold.compaction_requests or response is None:
        return "summary_request_event_id names no answered compaction request"
    text = "".join(part.text for part in response.content if isinstance(part, TextPart))
    if sha256_hex(text.encode("utf-8")) != event.data.summary_ref.sha256:
        return "summary_ref is not the text of the compaction request's response"
    return None


def _snapshot(fold: Fold, event: SnapshotEvent) -> None:
    # The quiescence predicate, C4 (spec/conformance/README.md, fork_points).
    expires = event.data.expires_at
    settled = all(status in ("committed", "resolved") for _, status in fold.effects.values())
    quiet = not (fold.in_turn or fold.pending or fold.parked)
    if quiet and settled and (expires is None or expires > fold.now):
        fold.fork_points.append((event.seq, event.event_id))


def _fork(fold: Fold, event: ForkEvent) -> None:
    # The last fork on the resolved chain is this branch's own.
    fold.repair = event.data.reason == "repair"


def _spawned(fold: Fold, event: AgentSpawnedEvent) -> None:
    fold.children[event.data.child_thread_id] = False


def _finished(fold: Fold, event: AgentFinishedEvent) -> ParseError | None:
    child = event.data.child_thread_id
    if fold.children.get(child) is not False:
        return reject(event, f"agent_finished for {child}, which is not a running child")
    fold.children[child] = True
    return None


def _todos(_fold: Fold, event: TodosUpdatedEvent) -> ParseError | None:
    ids = [todo.id for todo in event.data.todos]
    if len(set(ids)) != len(ids):
        return reject(event, "todo ids must be unique")
    return None


def _task_created(fold: Fold, event: TeamTaskCreatedEvent) -> None:
    fold.tasks[event.data.task_id] = Task("open", tuple(event.data.blocked_by))


def _task_claimed(fold: Fold, event: TeamTaskClaimedEvent) -> ParseError | None:
    task = fold.tasks.get(event.data.task_id)
    if task is None or task.status != "open":
        return reject(event, f"task {event.data.task_id} is not open")
    blockers = (fold.tasks.get(blocker) for blocker in task.blocked_by)
    if any(blocker is None or blocker.status != "completed" for blocker in blockers):
        return reject(event, f"task {event.data.task_id} has a blocker that is not completed")
    fold.tasks[event.data.task_id] = Task("claimed", task.blocked_by, event.data.member)
    return None


def _task_updated(fold: Fold, event: TeamTaskUpdatedEvent) -> ParseError | None:
    task = fold.tasks.get(event.data.task_id)
    if task is None or task.status != "claimed":
        return reject(event, f"task {event.data.task_id} is not claimed")
    status = event.data.status
    released = status == "released"
    fold.tasks[event.data.task_id] = (
        Task("open", task.blocked_by) if released else Task(status, task.blocked_by, task.owner)
    )
    return None


type _Keyed = TeamMessageEvent | ScheduleFiredEvent | ScheduleSkippedEvent | ChannelDeliveryEvent


def _unique(fold: Fold, event: _Keyed, kind: str, value: str) -> ParseError | None:
    if (kind, value) in fold.unique_keys:
        return reject(event, f"{kind} {value} is already used on this branch")
    fold.unique_keys.add((kind, value))
    return None


def _message(fold: Fold, event: TeamMessageEvent) -> ParseError | None:
    return _unique(fold, event, "message_id", event.data.message_id)


def _occurrence(fold: Fold, event: ScheduleFiredEvent | ScheduleSkippedEvent) -> ParseError | None:
    # An occurrence is logged once, fired or skipped.
    return _unique(fold, event, "occurrence_id", event.data.occurrence_id)


def _delivery(fold: Fold, event: ChannelDeliveryEvent) -> ParseError | None:
    return _unique(fold, event, "item_key", event.data.item_key)


HANDLERS: Mapping[type, Handler] = dict(
    [
        on(CompactedEvent, _compacted),
        on(SnapshotEvent, _snapshot),
        on(ForkEvent, _fork),
        on(AgentSpawnedEvent, _spawned),
        on(AgentFinishedEvent, _finished),
        on(TodosUpdatedEvent, _todos),
        on(TeamTaskCreatedEvent, _task_created),
        on(TeamTaskClaimedEvent, _task_claimed),
        on(TeamTaskUpdatedEvent, _task_updated),
        on(TeamMessageEvent, _message),
        on(ScheduleFiredEvent, _occurrence),
        on(ScheduleSkippedEvent, _occurrence),
        on(ChannelDeliveryEvent, _delivery),
    ]
)
