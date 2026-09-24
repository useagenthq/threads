"""team.events() without follow (spec/schema/README.md, "The feed"): a pure read of the team_feed
rows of the current epoch in offset order, each event as stored with its cursor and source. It
never writes and never drives the team."""

import sqlite3
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import now_ms
from threads.agents.team_handle_types import (
    EpochRestarted,
    MemberSource,
    OperatorSource,
    TeamCursor,
    TeamEvent,
    TeamItem,
    TeamLogSource,
    TeamSource,
)
from threads.log import (
    AskClosedEvent,
    BranchId,
    Event,
    MemberObservedEvent,
    MemberRef,
    MemberStartedEvent,
    MessagePolicyDecidedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    OperatorRefusedEvent,
    OperatorRequestEvent,
    OperatorSender,
    Principal,
    WaitFinishedEvent,
    WaitStartedEvent,
)
from threads.result import Err
from threads.store import SqliteStore
from threads.store.sql import int_of, text_of
from threads.team.rows import member_rows, team_row


def _feed(conn: sqlite3.Connection, team: str) -> list[tuple[int, int, str, int]]:
    rows: list[tuple[object, ...]] = conn.execute(
        "SELECT epoch, feed_offset, branch_id, seq FROM team_feed WHERE team_id = ?"
        " AND epoch = (SELECT MAX(epoch) FROM team_feed WHERE team_id = ?) ORDER BY feed_offset",
        (team, team),
    ).fetchall()
    return [(int_of(e), int_of(o), text_of(b), int_of(s)) for e, o, b, s in rows]


async def team_events(
    sq: SqliteStore, team: str, after: TeamCursor | None
) -> AsyncIterator[TeamItem]:
    """The committed feed after `after`, then the end."""
    rows = await sq.run(lambda c: _feed(c, team))
    if not rows:
        return
    epoch = rows[0][0]
    restarted = after is not None and after.epoch != epoch
    if restarted:
        yield EpochRestarted(TeamCursor(epoch, 0))
    start = 0 if after is None or restarted else after.offset
    sources = _Sources(sq, team)
    for _epoch, offset, branch, seq in rows:
        if offset > start:
            event, source = await sources.at(branch, seq)
            yield TeamEvent(TeamCursor(epoch, offset), source, event)


@dataclass(slots=True)
class _Sources:
    """Each feed row's event and where it was written, reading every branch once."""

    sq: SqliteStore
    team: str
    branches: dict[str, Sequence[Event]] = field(default_factory=dict[str, Sequence[Event]])
    requests: dict[str, str] = field(default_factory=dict[str, str])
    """The team log's events, by event id, attributed to their operator request."""
    principals: dict[str, Principal] = field(default_factory=dict[str, Principal])

    async def at(self, branch: str, seq: int) -> tuple[Event, TeamSource]:
        events = await self._events(branch)
        event = next((e for e in events if e.seq == seq), None)
        if event is None:
            raise AssertionError(f"the feed names {branch}@{seq}, not stored")
        team = await self.sq.run(lambda c: team_row(c, self.team))
        if team is None:
            raise AssertionError(f"no team {self.team}")
        if branch == team.team_log_branch_id:
            return event, self._operator(event)
        rows = await self.sq.run(lambda c: member_rows(c, self.team))
        row = next((r for r in rows if r.branch_id == branch), None)
        if row is None:
            raise AssertionError(f"the feed names {branch}, no member's")
        ref = MemberRef(
            tenant=team.tenant_id, team=self.team, name=row.name, generation=row.generation
        )
        return event, MemberSource(ref)

    async def _events(self, branch: str) -> Sequence[Event]:
        known = self.branches.get(branch)
        if known is not None:
            return known
        read = await self.sq.read(BranchId(branch), now_ms())
        if isinstance(read, Err):
            raise AssertionError(f"team log {branch}: {read.error.message}")
        events = read.value.fold.events
        self.branches[branch] = events
        # Attribution reads earlier team-log events: fold them all once, in order.
        for e in events:
            rid = self._request_of(e)
            if rid is not None:
                self.requests[e.event_id] = rid
            if isinstance(e, OperatorRequestEvent):
                self.principals[e.data.request_id] = e.data.principal
        return events

    def _operator(self, e: Event) -> TeamSource:
        rid = self.requests.get(e.event_id)
        principal = None if rid is None else self.principals.get(rid)
        if rid is None or principal is None:
            return TeamLogSource()
        return OperatorSource(principal, rid)

    def _request_of(self, e: Event) -> str | None:  # noqa: PLR0911 - one answer per event kind
        """The operator request a team-log event belongs to (spec/schema/README.md, "The feed")."""
        match e:
            case OperatorRequestEvent() | OperatorRefusedEvent():
                return e.data.request_id
            case MessagePolicyDecidedEvent():
                return None if e.data.request_id is MISSING else e.data.request_id
            case MemberStartedEvent():
                return self.requests.get(e.data.provenance.root_request.event_id)
            case MessageSentEvent():
                sender = e.data.envelope.from_
                return sender.operator if isinstance(sender, OperatorSender) else None
            case MessageReceivedEvent():
                return self._received(e)
            case MemberObservedEvent():
                return self._registered(e.data.monitor_id)
            case AskClosedEvent():
                return _keyed(e.data.ask_id)
            case WaitStartedEvent() | WaitFinishedEvent():
                return _keyed(e.data.wait_id)
            case _:
                return None

    def _received(self, e: MessageReceivedEvent) -> str | None:
        env = e.data.envelope
        if isinstance(env.ask_id, str):
            return _keyed(env.ask_id)
        if isinstance(env.monitor_id, str):
            return self._registered(env.monitor_id)
        return self.requests.get(env.provenance.root_request.event_id)

    def _registered(self, monitor: str) -> str | None:
        """A monitor's request: that of its registering event (`<branch>:<event_id>:<target>`)."""
        parts = monitor.split(":")
        return self.requests.get(parts[1]) if len(parts) > 1 else None


def _keyed(key: str) -> str | None:
    """An ask's or wait's request: its id is `<team log branch>:<request_id>`."""
    parts = key.split(":")
    return parts[1] if len(parts) > 1 else None
