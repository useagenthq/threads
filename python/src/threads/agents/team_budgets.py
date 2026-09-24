"""A member's budgets (spec/schema/README.md, "Teams"; design §2.6): before every model request of
a member turn the ledger reserves against the member's own thread budget, every ancestor thread's
budget through its structural parents (team_member, subagent, handoff) up to the root, and the run
budget of that turn's root request."""

from collections.abc import Awaitable, Callable

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import now_ms
from threads.log import (
    BranchId,
    Event,
    MessageReceivedEvent,
    Parent,
    RequestRef,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.loop.covering import Covering
from threads.result import Err
from threads.store import SqliteStore
from threads.team.rows import mail_envelope


async def ancestors_of(sq: SqliteStore, parent: Parent | None) -> tuple[Covering, ...]:
    """Every ancestor thread's own budget, from the member's parent up to the root."""
    out: list[Covering] = []
    at = parent
    while at is not None:
        read = await sq.read(BranchId(at.branch_id), now_ms())
        if isinstance(read, Err):
            break
        started = next(
            (e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None
        )
        if started is None:
            break
        policy = started.data.policy
        if policy is not MISSING and policy.budget is not MISSING:
            owner = ThreadId(at.thread_id)
            out.append(Covering(f"thread:{owner}", policy.budget, "ancestor", owner))
        at = None if started.data.parent is MISSING else started.data.parent
    return tuple(out)


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
