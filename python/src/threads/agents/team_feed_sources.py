"""Where each feed row's event was written (spec/schema/README.md, "The feed"): the member whose
branch it is, the operator request a team-log event belongs to, or the team log itself. Every
branch is read once, and a branch that grew since (a follower's) is read again, once."""

from dataclasses import dataclass, field

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import now_ms
from threads.agents.team_handle_types import (
    MemberSource,
    OperatorSource,
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
from threads.team.rows import TeamRow, member_rows, team_row


async def sources(sq: SqliteStore, team: str) -> "Sources":
    row = await sq.run(lambda c: team_row(c, team))
    if row is None:
        raise AssertionError(f"no team {team}")
    return Sources(sq, row, await members_of(sq, row))


async def members_of(sq: SqliteStore, team: TeamRow) -> dict[str, MemberRef]:
    """Each member branch of the team, as the ref its feed items name."""
    rows = await sq.run(lambda c: member_rows(c, team.team_id))
    return {
        r.branch_id: MemberRef(
            tenant=team.tenant_id, team=team.team_id, name=r.name, generation=r.generation
        )
        for r in rows
        if r.branch_id is not None
    }


@dataclass(slots=True)
class Sources:
    """Each feed row's event and where it was written, reading every branch once."""

    sq: SqliteStore
    team: TeamRow
    members: dict[str, MemberRef]
    branches: dict[str, dict[int, Event]] = field(default_factory=dict[str, dict[int, Event]])
    """Each branch read, its events by seq."""
    requests: dict[str, str] = field(default_factory=dict[str, str])
    """The team log's events, by event id, attributed to their operator request."""
    principals: dict[str, Principal] = field(default_factory=dict[str, Principal])

    async def at(self, branch: str, seq: int) -> tuple[Event, TeamSource]:
        event = (await self._events(branch)).get(seq)
        if event is None:
            # A follower's branch grew since it was read: read it again, once.
            self.branches.pop(branch, None)
            event = (await self._events(branch)).get(seq)
        if event is None:
            raise AssertionError(f"the feed names {branch}@{seq}, not stored")
        if branch == self.team.team_log_branch_id:
            return event, self._operator(event)
        if branch not in self.members:
            # A member materialized since the handle looked: read the rows again, once.
            self.members = await members_of(self.sq, self.team)
        ref = self.members.get(branch)
        if ref is None:
            raise AssertionError(f"the feed names {branch}, no member's")
        return event, MemberSource(ref)

    async def _events(self, branch: str) -> dict[int, Event]:
        known = self.branches.get(branch)
        if known is not None:
            return known
        read = await self.sq.read(BranchId(branch), now_ms())
        if isinstance(read, Err):
            raise AssertionError(f"team log {branch}: {read.error.message}")
        events = read.value.fold.events
        by_seq = {e.seq: e for e in events}
        self.branches[branch] = by_seq
        # Attribution reads earlier team-log events: fold them all once, in order.
        for e in events:
            rid = self._request_of(e)
            if rid is not None:
                self.requests[e.event_id] = rid
            if isinstance(e, OperatorRequestEvent):
                self.principals[e.data.request_id] = e.data.principal
        return by_seq

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
                prov = e.data.provenance
                return None if prov is MISSING else self.requests.get(prov.root_request.event_id)
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
