"""The team worker's deadline step for one ask or wait, under the asker's or waiter's writer
(spec/schema/README.md, "Teams"; design §4.8 and §4.12): it runs at or after the deadline, as one
transaction. A wait counts every settlement already committed (commit order, never timestamps: a
settlement counts iff its append deleted the monitor row first); an ask still answers a reply
that committed before its deadline. Reference: spec/tools/fixtures/ops_consume.py, deadline."""

from pydantic import JsonValue

from threads.log import WaitStartedEvent
from threads.reduce import Fold
from threads.store.conn import Conn
from threads.team.cancel import apply_cancel
from threads.team.close import CloseContext, committed_notices, complete_ask, finish_wait, mine
from threads.team.rows import ask_row, due_asks, team_of_log
from threads.team.view import ask_open, open_waits

_NOT_DUE: dict[str, JsonValue] = {"status": "not_due"}
_NEVER = 2**63 - 1


def _started(fold: Fold, wait_id: str) -> WaitStartedEvent | None:
    return next(
        (e for e in fold.events if isinstance(e, WaitStartedEvent) and e.data.wait_id == wait_id),
        None,
    )


def deadline(ctx: CloseContext, ident: str) -> JsonValue:
    """The deadline step for `ident`, an AskId or a WaitId of this writer. A cancel pending for
    the writer is applied here too, as a consume would: its barrier closes the ask or finishes the
    wait."""
    ask = ask_row(ctx.conn, ident)
    if ask is not None:
        return _ask_due(ctx, ident, ask.deadline)
    started = _started(ctx.fold, ident)
    due = (
        started is not None
        and ctx.batch.now >= started.data.deadline
        and ident in open_waits(ctx.fold, ctx.batch)
    )
    if not due:
        return _NOT_DUE
    cancel = next((m for m in mine(ctx) if m.kind == "cancel"), None)
    if cancel is not None:
        apply_cancel(ctx, cancel)
        return {"wait_id": ident, "status": "cancelled"}
    committed_notices(ctx, ident)
    return finish_wait(ctx, ident, cause=None, deadline=True)


def _ask_due(ctx: CloseContext, ident: str, due: int) -> JsonValue:
    if not ask_open(ctx.conn, ctx.branch_id, ident, ctx.batch):
        return _NOT_DUE
    # Team close is a trigger of its own: the team log's next step closes its open asks.
    log = team_of_log(ctx.conn, ctx.branch_id)
    if log is not None and log.closed_at is not None:
        closed = complete_ask(ctx, ident, cancelled=True, due=False)
    elif ctx.batch.now < due:
        return _NOT_DUE
    else:
        pending = mine(ctx)
        answered = any(m.kind in ("reply", "bounce") and m.ask_id == ident for m in pending)
        cancel = next((m for m in pending if m.kind == "cancel"), None)
        if cancel is not None and not answered:
            # The barrier closes the ask cancelled, not timed out.
            apply_cancel(ctx, cancel)
            return {"ask_id": ident, "status": "cancelled"}
        closed = complete_ask(ctx, ident, cancelled=False, due=True)
    return _NOT_DUE if closed is None else closed


def due_ids(conn: Conn, fold: Fold, branch: str, now: int) -> list[str]:
    """This writer's asks and waits whose deadline has passed by `now`."""
    asks = [a for a, _ in due_asks(conn, branch, now)]
    waits = [
        e.data.wait_id
        for e in fold.events
        if isinstance(e, WaitStartedEvent)
        and e.data.wait_id in fold.team.waits
        and now >= e.data.deadline
    ]
    return asks + waits


def next_deadline(conn: Conn, fold: Fold, branch: str) -> int | None:
    """The earliest deadline among this writer's open asks and waits, if any."""
    asks = [d for _, d in due_asks(conn, branch, _NEVER)]
    waits = [s.data.deadline for w in fold.team.waits if (s := _started(fold, w)) is not None]
    found = asks + waits
    return min(found) if found else None
