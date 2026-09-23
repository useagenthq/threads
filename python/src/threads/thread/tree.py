"""Tree-wide cancellation (spec/schema/README.md, "Subagent cancellation and parking"; ): cancelling a thread bars every descendant subagent that hasn't finished.

The thread's own `cancel_requested` is the barrier. Then every child its log spawned without an
`agent_finished` gets `cancel_requested{scope: tree, reason: "ancestor cancelled"}` (actor host,
principal the canceller), recursively. A child whose thread doesn't exist yet gets nothing: it is
never started. A child whose turn is closed, or already barred, is left as it is.
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store, now_ms, open_store
from threads.log import BranchId, ParseError, Principal, ThreadId, ThreadStartedEvent
from threads.loop.drive import open_cancel
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.store import Draft
from threads.store.companion import Companion
from threads.thread.control import Controlled, append, barred, cancel, first

if TYPE_CHECKING:
    from pydantic import JsonValue

REASON: Final = "ancestor cancelled"


async def cancel_tree(
    store: Store, branch: BranchId, principal: Principal, companion: Companion | None = None
) -> Controlled:
    """Thread.cancel: the thread's barrier, then its unfinished descendants'."""
    done = await cancel(store, branch, principal, companion=companion)
    if isinstance(done, Ok):
        await cancel_children(store, branch, principal)
    return done


async def cancel_children(store: Store, branch: BranchId, principal: Principal) -> None:
    read = await (await open_store(store)).read(branch, now_ms())
    if isinstance(read, Err):
        return
    for child in unfinished(read.value.fold):
        await bar_child(store, child, principal)


def unfinished(fold: Fold) -> list[ThreadId]:
    """The children the log spawned that have no agent_finished yet."""
    return [child for child, done in fold.children.items() if not done]


async def bar_child(store: Store, child: ThreadId, principal: Principal) -> None:
    """The child's tree barrier, when its turn is open and not barred yet, then its own
    children's. A refusal (the child's lease held elsewhere) is left to that child's run: its
    parent bars it again before running it on."""
    sq = await open_store(store)
    root = await sq.root(child)
    if isinstance(root, Err):
        return
    data: dict[str, JsonValue] = {"scope": "tree", "reason": REASON}
    by: dict[str, JsonValue] = {"kind": "host", "principal": to_json(principal)}
    barrier = first("cancel_requested", data, by)

    def build(fold: Fold) -> Ok[Sequence[Draft]] | Err[ParseError]:
        if not fold.in_turn or open_cancel(fold.events) is not None:
            return Err(ParseError("not_found", "nothing to stop"))
        return Ok(barred(fold, barrier))

    await append(store, root.value, build)
    await cancel_children(store, root.value, principal)


async def root_of(
    store: Store, thread_id: ThreadId, through: tuple[str, ...] = ("subagent",)
) -> tuple[ThreadId, BranchId] | None:
    """The thread at the top of a subagent's tree (a thread that is no subagent is its own
    root), at the branch its child was spawned from; `through` also handoff parents for the
    root run of approval authority."""
    sq = await open_store(store)
    root = await sq.root(thread_id)
    if not isinstance(root, Ok):
        return None
    at = (thread_id, root.value)
    while True:
        read = await sq.read(at[1], 0)
        if not isinstance(read, Ok):
            return None
        events = read.value.fold.events
        started = next((e for e in events if isinstance(e, ThreadStartedEvent)), None)
        parent = MISSING if started is None else started.data.parent
        if parent is MISSING or parent.relation not in through:
            return at
        at = (parent.thread_id, parent.branch_id)
