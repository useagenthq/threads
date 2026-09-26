"""A thread's visible branches, each with the mode its runs use (spec/api.json Thread.branches).
A branch whose resolved chain holds a stub fork reports `stub`, so a reopened stub child is
listed as one."""

from threads._generated.host_api_v1 import BranchInfo
from threads.agents.store import now_ms
from threads.log import BranchId, ThreadId
from threads.result import Ok
from threads.store import SqliteStore
from threads.thread.frozen_stubs import stub_fork_ref


async def listed(sq: SqliteStore, thread_id: ThreadId) -> tuple[BranchInfo, ...]:
    """Every listed branch of the thread; a forking or failed fork is never listed."""
    rows = await sq.tables.branches(thread_id)
    out: list[BranchInfo] = []
    for r in rows:
        mode = "stub" if await stubbed(sq, r.branch_id, r.parent_branch_id) else "live"
        parent = {} if r.parent_branch_id is None else {"parent_branch_id": r.parent_branch_id}
        at = {} if r.fork_at_seq is None else {"fork_at_seq": r.fork_at_seq}
        out.append(
            BranchInfo.model_validate(
                {"branch_id": r.branch_id, "mode": mode, "runnable": r.state == "ready"}
                | parent
                | at
            )
        )
    return tuple(out)


async def stubbed(sq: SqliteStore, branch: BranchId, parent: str | None) -> bool:
    """Whether a listed branch runs stubbed: its chain holds a fork that froze a stub script.
    A root branch never does, so the common case reads nothing."""
    if parent is None:
        return False
    read = await sq.read(branch, now_ms())
    return isinstance(read, Ok) and stub_fork_ref(read.value.fold.events) is not None
