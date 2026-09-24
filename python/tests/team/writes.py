"""The write path of a recorded team: its logs appended again, event by event, through real
writers. The lead's first append opens the team log (so the recorded team_opened is not appended
again), and every other log opens with branch.open."""

from collections.abc import Sequence
from dataclasses import dataclass

from team.team_kit import appendable

from threads.log import Event, TeamOpenedEvent
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store import Draft, SqliteStore, VerifiedLog, Writer

HOLDER = "replay"


@dataclass
class ReplayClock:
    """Follows each event's own time, which moves back and forth between logs."""

    now: int = 0

    def __call__(self) -> int:
        return self.now


@dataclass
class _Queue:
    log: VerifiedLog
    team_log: bool
    events: list[Event]
    writer: Writer | None = None


def draft_of(e: Event) -> Draft:
    """The event as the draft that appended it: the writer fills in the rest again."""
    wire = to_json(e)
    assert isinstance(wire, dict)
    data, actor = wire["data"], wire["actor"]
    assert isinstance(data, dict)
    assert isinstance(actor, dict)
    return Draft(e.type, data, actor, e.critical, e.event_id)


async def reappend(store: SqliteStore, logs: Sequence[VerifiedLog]) -> None:
    """Re-appends `logs` into `store` through writers, in an order their rows allow."""
    clock = ReplayClock()
    queues: list[_Queue] = []
    for log in logs:
        events = list(log.fold.events)
        team_log = bool(events) and isinstance(events[0], TeamOpenedEvent)
        queues.append(_Queue(log, team_log, events[1:] if team_log else events))
    moved = True
    while moved:
        moved = False
        for q in queues:
            moved = await _drain(store, clock, q) or moved
    assert [len(q.events) for q in queues] == [0] * len(queues)


async def _drain(store: SqliteStore, clock: ReplayClock, q: _Queue) -> bool:
    """Appends what of `q` can go now: each event once the rows it moves exist."""
    moved = False
    while q.events and await store.run(lambda c, e=q.events[0]: appendable(c, e)):
        clock.now = q.events[0].time
        if not await _append(store, clock, q, q.events[0]):
            break
        q.events.pop(0)
        moved = True
    return moved


async def _append(store: SqliteStore, clock: ReplayClock, q: _Queue, e: Event) -> bool:
    """Appends `e` to its log, opening the log's branch with it when it is the first event.
    False: a team log the lead's first append has not opened yet."""
    header = q.log.segments[0].header
    if q.writer is None and q.team_log:
        if not isinstance(await store.branch(header.branch_id), Ok):
            return False
        taken = await store.acquire(header.branch_id, HOLDER, clock)
        assert isinstance(taken, Ok), taken
        q.writer = taken.value
    if q.writer is not None:
        assert isinstance(await q.writer.renew(), Ok)
        done = await q.writer.append([draft_of(e)])
        assert isinstance(done, Ok), (header.branch_id, e.seq, done)
        return True
    opened = await store.open_branch(
        header.thread_id, header.branch_id, [draft_of(e)], holder_id=HOLDER, clock=clock
    )
    assert isinstance(opened, Ok), opened
    assert isinstance(opened.value, Writer), opened
    q.writer = opened.value
    return True
