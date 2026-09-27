"""Opening a tenant's host team, lazily and idempotently (spec/schema/README.md, "Teams Phase 2").

Its ids are derived, so two processes racing the open collide on the branch's primary key and the
loser gets `already_open` — the required behaviour, not an error. The open carries
`team_opened{kind: host, tenant}` and one `member_started{host_member}` per configured member, so
one commit leaves the whole team; a name configured later is added by an append afterwards.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from threads.log import ParseError
from threads.result import Err, Ok
from threads.store import SqliteStore
from threads.store.lines import Draft, uuid7
from threads.store.worker import Clock
from threads.team.host_team import HostTeamIds, host_team_ids
from threads.team.rows import member_rows

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class HostMemberStart:
    """What a host member's start records: its agent name (which is its member name), the
    config_hash of its pin as a host member, and the thread its materialize will open."""

    agent: str
    config_hash: str
    thread_id: str


def start_draft(ids: HostTeamIds, tenant: str, start: HostMemberStart) -> Draft:
    """member_started{host_member}: no parent, no provenance, no label, no budget, no task."""
    member: JsonValue = {
        "tenant": tenant,
        "team": ids.team,
        "name": start.agent,
        "generation": 1,
    }
    data: dict[str, JsonValue] = {
        "member": member,
        "agent": start.agent,
        "config_hash": start.config_hash,
        "thread_id": start.thread_id,
        "host_member": True,
    }
    return Draft("member_started", data)


async def ensure_host_team(
    sq: SqliteStore,
    tenant: str,
    members: Mapping[str, str],
    *,
    holder: str,
    clock: Clock,
) -> Ok[HostTeamIds] | Err[ParseError]:
    """The tenant's host team, opened if it is not there yet, with a row for every configured
    member. `members` maps each host member's agent name to its config_hash. Idempotent: a
    concurrent open is `already_open`, and a name that already has a row is left alone."""
    ids = host_team_ids(tenant)
    starts = [HostMemberStart(name, config, uuid7(clock())) for name, config in members.items()]
    opened_team: JsonValue = {"team": ids.team, "kind": "host", "tenant": tenant}
    drafts = [
        Draft("team_opened", opened_team),
        *(start_draft(ids, tenant, s) for s in starts),
    ]
    opened = await sq.open_branch(
        ids.log_thread_id, ids.log_branch_id, drafts, holder_id=holder, clock=clock
    )
    if isinstance(opened, Err):
        return opened
    writer = opened.value
    if not isinstance(writer, str):
        await writer.release()
        return Ok(ids)
    return await _add_missing(sq, _Open(ids, tenant, holder, clock), starts)


@dataclass(frozen=True, slots=True)
class _Open:
    ids: HostTeamIds
    tenant: str
    holder: str
    clock: Clock


async def _add_missing(
    sq: SqliteStore, at: _Open, starts: Sequence[HostMemberStart]
) -> Ok[HostTeamIds] | Err[ParseError]:
    """A name configured after the team opened gets its row in an append of its own."""
    rows = await sq.run(lambda c: member_rows(c, at.ids.team))
    known = {r.name for r in rows}
    missing = [s for s in starts if s.agent not in known]
    if not missing:
        return Ok(at.ids)
    taken = await sq.acquire(at.ids.log_branch_id, at.holder, at.clock)
    if isinstance(taken, Err):
        # Another process holds the team log: it is opening the same rows, or will next pass.
        return Ok(at.ids)
    writer = taken.value
    try:
        done = await writer.append([start_draft(at.ids, at.tenant, s) for s in missing])
    finally:
        await writer.release()
    return Ok(at.ids) if isinstance(done, Ok) else Err(done.error)
