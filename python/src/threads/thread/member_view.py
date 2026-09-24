"""Which member a team member's thread is, for approval views (host-api PendingApproval.member):
its name, its start's label and, for a dynamic agent, what its starter chose. A read only."""

from typing import TYPE_CHECKING

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.host_api_v1 import Member, PendingApproval
from threads.agents.store import now_ms
from threads.log import MemberStartedEvent, ThreadStartedEvent
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import SqliteStore

if TYPE_CHECKING:
    from pydantic import JsonValue


async def member_of(sq: SqliteStore, fold: Fold) -> Member | None:
    """The member_started that started this thread: the lead's (its parent names it) or, for an
    operator start, the team log's. None outside a team member's thread."""
    started = next((e for e in fold.events if isinstance(e, ThreadStartedEvent)), None)
    if started is None or started.data.parent is MISSING:
        return None
    parent = started.data.parent
    if parent.relation != "team_member":
        return None
    lead = await sq.read(parent.branch_id, now_ms())
    if isinstance(lead, Err):
        return None
    found = _started(lead.value.fold, started.thread_id)
    head = next((e for e in lead.value.fold.events if isinstance(e, ThreadStartedEvent)), None)
    if found is None and head is not None and head.data.team is not MISSING:
        log = await sq.read(head.data.team.log_branch_id, now_ms())
        found = None if isinstance(log, Err) else _started(log.value.fold, started.thread_id)
    if found is None:
        return None
    d = found.data
    view: dict[str, JsonValue] = {"name": d.member.name}
    if d.label is not MISSING:
        view["label"] = d.label
    if d.define is not MISSING:
        view["define"] = to_json(d.define)
    return Member.model_validate(view)


def _started(fold: Fold, thread: str) -> MemberStartedEvent | None:
    return next(
        (
            e
            for e in fold.events
            if isinstance(e, MemberStartedEvent) and e.data.thread_id == thread
        ),
        None,
    )


def with_member(
    pending: tuple[PendingApproval, ...], member: Member | None
) -> tuple[PendingApproval, ...]:
    """Each open challenge with the member it was raised in."""
    if member is None:
        return pending
    return tuple(p.model_copy(update={"member": member}) for p in pending)
