"""The write path of a recorded team: its logs appended again, event by event, through real
writers. The lead's first append opens the team log (so the recorded team_opened is not appended
again), and every other log opens with branch.open."""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from pydantic import JsonValue, TypeAdapter
from team.team_kit import appendable

from threads.log import Event, TeamOpenedEvent
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store import Draft, SqliteStore, VerifiedLog, Writer

HOLDER = "replay"
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


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


@dataclass
class _Replay:
    store: SqliteStore
    clock: ReplayClock
    renamed: dict[str, str] = field(default_factory=dict[str, str])
    """A team log's thread id is minted when the lead's first append opens it (only its branch
    id is logged), so each recorded team log thread is renamed to the one minted."""


def renamed(value: JsonValue, ids: Mapping[str, str]) -> JsonValue:
    """`value` with every renamed id replaced."""
    text = json.dumps(value)
    for recorded, minted in ids.items():
        text = text.replace(recorded, minted)
    return _JSON.validate_json(text)


def draft_of(e: Event, ids: Mapping[str, str] | None = None) -> Draft:
    """The event as the draft that appended it: the writer fills in the rest again."""
    wire = renamed(to_json(e), ids or {})
    assert isinstance(wire, dict)
    data, actor = wire["data"], wire["actor"]
    assert isinstance(data, dict)
    assert isinstance(actor, dict)
    return Draft(e.type, data, actor, e.critical, e.event_id)


async def reappend(store: SqliteStore, logs: Sequence[VerifiedLog]) -> dict[str, str]:
    """Re-appends `logs` into `store` through writers, in an order their rows allow. Returns
    each recorded team log thread's minted id."""
    replay = _Replay(store, ReplayClock())
    queues: list[_Queue] = []
    for log in logs:
        events = list(log.fold.events)
        team_log = bool(events) and isinstance(events[0], TeamOpenedEvent)
        queues.append(_Queue(log, team_log, events[1:] if team_log else events))
    moved = True
    while moved:
        moved = False
        for q in queues:
            moved = await _drain(replay, q) or moved
    assert [len(q.events) for q in queues] == [0] * len(queues)
    return replay.renamed


async def _drain(replay: _Replay, q: _Queue) -> bool:
    """Appends what of `q` can go now: each event once the rows it moves exist."""
    moved = False
    while q.events and await replay.store.run(lambda c, e=q.events[0]: appendable(c, e)):
        replay.clock.now = q.events[0].time
        if not await _append(replay, q, q.events[0]):
            break
        q.events.pop(0)
        moved = True
    return moved


async def _append(replay: _Replay, q: _Queue, e: Event) -> bool:
    """Appends `e` to its log, opening the log's branch with it when it is the first event.
    False: a team log the lead's first append has not opened yet."""
    header = q.log.segments[0].header
    if q.writer is None and q.team_log:
        q.writer = await _take_team_log(replay, q)
        if q.writer is None:
            return False
    draft = draft_of(e, replay.renamed)
    if q.writer is not None:
        assert isinstance(await q.writer.renew(), Ok)
        done = await q.writer.append([draft])
        assert isinstance(done, Ok), (header.branch_id, e.seq, done)
        return True
    opened = await replay.store.open_branch(
        header.thread_id, header.branch_id, [draft], holder_id=HOLDER, clock=replay.clock
    )
    assert isinstance(opened, Ok), opened
    assert isinstance(opened.value, Writer), opened
    q.writer = opened.value
    return True


async def _take_team_log(replay: _Replay, q: _Queue) -> Writer | None:
    """The team log the lead's first append opened, once it has; learns its minted thread id."""
    header = q.log.segments[0].header
    stored = await replay.store.read(header.branch_id, replay.clock())
    if not isinstance(stored, Ok):
        return None
    replay.renamed[header.thread_id] = stored.value.segments[0].header.thread_id
    taken = await replay.store.acquire(header.branch_id, HOLDER, replay.clock)
    assert isinstance(taken, Ok), taken
    return taken.value
