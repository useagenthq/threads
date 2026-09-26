"""A member's budgets (spec/schema/README.md, "Teams"; design §2.6): before every model request of
a member turn the ledger reserves against the member's own thread budget, every ancestor thread's
budget through its structural parents (team_member, subagent, handoff) up to the root, and the run
budget of that turn's root request."""

from collections.abc import Awaitable, Callable

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import now_ms
from threads.log import (
    BranchId,
    Event,
    MemberStartedEvent,
    MessageReceivedEvent,
    ModelRef,
    Parent,
    Policy,
    RequestRef,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.loop.covering import Covering
from threads.loop.team_runtime import TeamRecipient
from threads.result import Err
from threads.store import SqliteStore
from threads.team.rows import MemberRow, mail_envelope, team_row


async def ancestors_of(sq: SqliteStore, parent: Parent | None) -> tuple[Covering, ...]:
    """Every ancestor thread's own budget, from the member's parent up to the root."""
    return await _ancestors(sq, None if parent is None else (parent.thread_id, parent.branch_id))


type _At = tuple[str, str]
"""(thread_id, branch_id) of a structural parent."""


async def started_cap(sq: SqliteStore, parent: Parent | None) -> tuple[Covering, ...]:
    """The cap a start put on the member (Team.start's budget, capped by the message_policy rule's):
    member_started.budget, a budget of the member's own thread beside the one its pin carries. The
    pin is hashed, so a per-start cap can only live here."""
    if parent is None:
        return ()
    read = await sq.read(BranchId(parent.branch_id), now_ms())
    if isinstance(read, Err):
        return ()
    started = next(
        (
            e
            for e in read.value.fold.events
            if isinstance(e, MemberStartedEvent) and e.event_id == parent.event_id
        ),
        None,
    )
    if started is None or started.data.budget is MISSING:
        return ()
    return (Covering(f"start:{parent.event_id}", started.data.budget, "thread"),)


async def _ancestors(sq: SqliteStore, at: _At | None) -> tuple[Covering, ...]:
    out: list[Covering] = []
    while at is not None:
        thread, branch = at
        read = await sq.read(BranchId(branch), now_ms())
        if isinstance(read, Err):
            break
        started = next(
            (e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None
        )
        if started is None:
            break
        policy = started.data.policy
        if policy is not MISSING and policy.budget is not MISSING:
            owner = ThreadId(thread)
            out.append(Covering(f"thread:{owner}", policy.budget, "ancestor", owner))
        parent = started.data.parent
        at = None if parent is MISSING else (parent.thread_id, parent.branch_id)
    return tuple(out)


class _Pinned(BaseModel):
    """What one request of a member reserves, from its pinned config (a strict subset)."""

    model_config = ConfigDict(extra="ignore", frozen=True, strict=True)
    model: ModelRef
    model_params: dict[str, JsonValue]
    policy: Policy | None = None


type _Recipient = tuple[_Pinned, _At | None, tuple[Covering, ...]]
"""A member's pin, its structural parent, and the cap its start put on it."""


def recipient_of(sq: SqliteStore) -> Callable[[MemberRow], Awaitable[TeamRecipient | None]]:
    """An asked member's budgets (ask's headroom): its own thread budget and every ancestor's,
    with what one request of its model reserves, from its thread_started; a member still starting
    has no log yet, so from its pinned config, under its lead."""

    async def recipient(row: MemberRow) -> TeamRecipient | None:
        got = await (_configured(sq, row) if row.branch_id is None else _started(sq, row.branch_id))
        if got is None:
            return None
        pinned, parent, cap = got
        policy = pinned.policy
        own = () if policy is None or policy.budget is MISSING else (policy.budget,)
        mine = tuple(Covering(f"thread:{row.thread_id}", b, "thread") for b in own)
        covering = (*mine, *cap, *await _ancestors(sq, parent))
        return TeamRecipient(pinned.model, pinned.model_params, policy, covering)

    return recipient


async def _started(sq: SqliteStore, branch: str) -> _Recipient | None:
    read = await sq.read(BranchId(branch), now_ms())
    if isinstance(read, Err):
        return None
    started = next((e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None)
    if started is None:
        return None
    data = started.data
    policy = None if data.policy is MISSING else data.policy
    pinned = _Pinned(model=data.model, model_params=dict(data.model_params), policy=policy)
    parent = None if data.parent is MISSING else (data.parent.thread_id, data.parent.branch_id)
    cap = await started_cap(sq, None if data.parent is MISSING else data.parent)
    return pinned, parent, cap


async def _configured(sq: SqliteStore, row: MemberRow) -> _Recipient | None:
    """A member still starting: its start already checked its cap, so only the pin and its
    ancestors are read here."""
    config = await sq.get_artifact(row.config_hash)
    team = await sq.run(lambda c: team_row(c, row.team_id))
    lead = None if team is None else await sq.root(ThreadId(team.lead_thread_id))
    if isinstance(config, Err) or team is None or lead is None or isinstance(lead, Err):
        return None
    at = (team.lead_thread_id, lead.value)
    return _Pinned.model_validate_json(config.value), at, ()


def run_covering(sq: SqliteStore) -> Callable[[Event], Awaitable[Covering | None]]:
    """The run budget of the request a member turn belongs to: the root request of its opener's
    provenance (a receipt's, or its task's), when that request is a user_input with a budget. An
    operator request's run budget only attributes in Phase 1."""
    found: dict[str, Covering | None] = {}

    async def covering(opener: Event) -> Covering | None:
        root = await _root_of(sq, opener)
        if root is None:
            return None
        key = f"run:{root.thread_id}:{root.event_id}"
        if key not in found:
            found[key] = await _budget_of(sq, root, key)
        return found[key]

    return covering


async def _root_of(sq: SqliteStore, opener: Event) -> RequestRef | None:
    if isinstance(opener, MessageReceivedEvent):
        return opener.data.envelope.provenance.root_request
    if not isinstance(opener, UserInputEvent) or opener.data.mail_id is MISSING:
        return None
    mail = opener.data.mail_id
    env = await sq.run(lambda c: mail_envelope(c, mail))
    return None if env is None else env.provenance.root_request


async def _budget_of(sq: SqliteStore, root: RequestRef, budget_id: str) -> Covering | None:
    branch = await sq.root(ThreadId(root.thread_id))
    if isinstance(branch, Err):
        return None
    read = await sq.read(branch.value, now_ms())
    if isinstance(read, Err):
        return None
    event = next((e for e in read.value.fold.events if e.event_id == root.event_id), None)
    if not isinstance(event, UserInputEvent) or event.data.budget is MISSING:
        return None
    return Covering(budget_id, event.data.budget, "run")
