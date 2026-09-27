"""The supervisor step (spec/schema/README.md, "Teams Phase 2" E, semantic rule 51): the host team
log's writer decides once on each ended host member generation, and a restart starts the next one
in the same append. Reference: spec/tools/fixtures/ops_supervise.py::supervise.

Exactly once is the fold inside the append's transaction: `decided` holds every generation this log
has already decided, so a second host that reaches the same end appends nothing. A host that
decided from a fold that went stale is refused by rule 51 and rolls back.
"""

from dataclasses import dataclass
from typing import Literal, TypeGuard

from pydantic import JsonValue

from threadsai.log import MemberEndedEvent, parse_log_line
from threadsai.reduce.fold import Fold
from threadsai.reduce.rules_super import generation_key, restarts_in_window
from threadsai.result import Ok
from threadsai.store.conn import Conn
from threadsai.store.lines import Draft
from threadsai.store.sql import blob_of, int_of, text_of
from threadsai.team.batch import Batch
from threadsai.team.call import Refusal
from threadsai.team.request import Request
from threadsai.team.rows import MemberRow, TeamRow, member_named

type Action = Literal["restart", "stop"]


@dataclass(frozen=True, slots=True)
class RestartPolicy:
    """`members.<name>` as the host resolved it (spec/api.json `HostMemberOptions`)."""

    restart: Literal["on_failure", "never"] = "on_failure"
    max_restarts: int = 3
    within_ms: int = 60_000

    def on_wire(self) -> dict[str, JsonValue]:
        return {
            "restart": self.restart,
            "max_restarts": self.max_restarts,
            "within_ms": self.within_ms,
        }


@dataclass(frozen=True, slots=True)
class Supervised:
    """What the step did: nothing_ended, already_decided, or the decision it recorded."""

    status: Literal["nothing_ended", "already_decided", "decided"]
    action: Action | None = None
    restarts_in_window: int | None = None


@dataclass(frozen=True, slots=True)
class SuperviseContext:
    """What the decision reads and writes, inside the host team log's own append."""

    conn: Conn
    fold: Fold
    """The team log's committed fold: its `host.decided` makes the decision exactly once."""
    batch: Batch
    team: TeamRow
    thread_id: str
    """The next generation's thread, minted before the append; unused by a stop."""
    config_hash: str | None = None
    """The agent's pin as the host holds it now, stored before this append. A restart re-pins, so
    an agent that was unregistered or changed comes back on the definition that is live: that is
    what makes a restart a recovery and not a repeat. None (the op vectors, which have no
    registry, and a name the host no longer defines): the ended generation's own pin, which fails
    its rebind again and stops the member at the cap."""


@dataclass(frozen=True, slots=True)
class _End:
    branch: str
    seq: int
    failed: bool


def supervise(ctx: SuperviseContext, name: str, policy: RestartPolicy) -> Supervised:
    """One decision on the named host member's ended generation. `restart` needs the policy to
    allow it, the window to hold fewer restarts than the cap, and that end to be `failed`: a
    rebind failure. An own-budget end and a cancel are stops, and `restart="never"` records one."""
    row = member_named(ctx.conn, ctx.team.team_id, name)
    if row is None or row.role != "host_member" or row.state != "ended":
        return Supervised("nothing_ended")
    host = ctx.fold.host
    if generation_key(name, row.generation) in host.decided:
        return Supervised("already_decided")
    end = _ended_at(ctx.conn, row.branch_id)
    count = restarts_in_window(host, name, ctx.batch.now, policy.within_ms)
    allowed = policy.restart == "on_failure" and count < policy.max_restarts
    action: Action = "restart" if allowed and end.failed else "stop"
    member: dict[str, JsonValue] = {
        "tenant": ctx.team.tenant_id,
        "team": ctx.team.team_id,
        "name": name,
        "generation": row.generation,
    }
    decided: dict[str, JsonValue] = {
        "member": member,
        "ended": {"branch_id": end.branch, "seq": end.seq},
        "action": action,
        "restarts_in_window": count,
        "policy": policy.on_wire(),
    }
    ctx.batch.add(Draft("supervisor_decided", decided))
    if action == "restart":
        ctx.batch.add(Draft("member_started", _restart(ctx, member, row)))
    return Supervised("decided", action, count)


def _restart(
    ctx: SuperviseContext, member: dict[str, JsonValue], row: MemberRow
) -> dict[str, JsonValue]:
    """The next generation's start: a new empty thread, on the pin the host holds now."""
    return {
        "member": {**member, "generation": row.generation + 1},
        "agent": row.agent,
        "config_hash": ctx.config_hash or row.config_hash,
        "thread_id": ctx.thread_id,
        "host_member": True,
        "restart_of": row.generation,
    }


def _ended_at(conn: Conn, branch: str | None) -> _End:
    """The member_ended of an ended generation's own log: where it is, and whether it failed."""
    found = conn.execute(
        "SELECT branch_id, seq, line FROM events"
        " WHERE branch_id = ? AND type = 'member_ended' ORDER BY seq DESC LIMIT 1",
        (branch,),
    ).fetchone()
    if found is None:
        raise AssertionError(f"an ended member has a member_ended: branch {branch}")
    branch_id, seq, line = found
    parsed = parse_log_line(blob_of(line).decode("utf-8", "surrogatepass"))
    event = parsed.value if isinstance(parsed, Ok) else None
    if not isinstance(event, MemberEndedEvent):
        raise AssertionError(f"an ended member has a member_ended: branch {branch}")
    return _End(text_of(branch_id), int_of(seq), event.data.result.status == "failed")


def restart(
    req: Request, name: str, fold: Fold, thread_id: str, config_hash: str | None = None
) -> dict[str, JsonValue]:
    """The operator's half of rule 51: `Team.start(name)` on a host team starts the next
    generation of a host member the supervisor stopped (spec/api.json `Team.start`, "Teams Phase
    2" E). Anything else — a name that is no host member of this team, a generation that is still
    live, or one the supervisor restarted rather than stopping — is `forbidden`, and the request
    records the refusal."""
    denied = req.decide("start", name)
    if denied is not None:
        return req.refuse(denied)
    row = member_named(req.conn, req.team.team_id, name)
    if not _stopped(row, fold):
        return req.refuse(Refusal("forbidden"))
    member: dict[str, JsonValue] = {
        "tenant": req.team.tenant_id,
        "team": req.team.team_id,
        "name": name,
        "generation": row.generation + 1,
    }
    data: dict[str, JsonValue] = {
        "member": member,
        "agent": row.agent,
        "config_hash": config_hash or row.config_hash,
        "thread_id": thread_id,
        "host_member": True,
        "restart_of": row.generation,
        # Rule 51: an operator restart's provenance names the operator_request that asked for it.
        "provenance": req.provenance,
    }
    req.batch.add(Draft("member_started", data))
    return req.done({"member": member, "status": "started"})


def _stopped(row: MemberRow | None, fold: Fold) -> TypeGuard[MemberRow]:
    """The name is a host member of this team whose current generation the supervisor stopped."""
    return (
        row is not None
        and row.role == "host_member"
        and row.state == "ended"
        and fold.host.decided.get(generation_key(row.name, row.generation)) == "stop"
    )
