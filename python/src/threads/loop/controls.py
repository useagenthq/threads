"""The holder's side of a cross-process cancel (lane 29F).

The lease holder applies the thread's durable control items at its own step boundaries, in the
same transaction that consumes them, so a cancel another process asked for lands before this run's
next dispatch. A run that takes a free lease does it on its first boundary too, which is how a
cancel left behind by a crash is applied before anything else. `consumed_seq IS NULL` is the CAS:
exactly one owner applies each item, and a loser's whole append rolls back.
"""

from collections.abc import Sequence

from threads.log import ThreadId
from threads.loop.runtime import Halt, Runtime, lost
from threads.result import Err
from threads.store import Draft
from threads.store.writer import DecideTx, Refusal
from threads.thread import control_items
from threads.thread.control import barred, soft_stop, thread_barrier


async def take_control_items(rt: Runtime) -> Halt | None:
    """Applies every control item waiting for this thread, once. None: nothing was waiting."""
    thread = rt.fold.thread_id
    if thread is None:
        return None
    at = ThreadId(thread)
    # Read-only: the boundary check runs on every step, and it must take no write lock.
    if not await rt.store.run(lambda c: control_items.waiting(c, at), read_only=True):
        return None

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        items = control_items.pending(tx.conn, at)
        drafts = [d for item in items for d in _applied(tx, item)]
        if not drafts:
            return ()
        # Consumed at this append's first event, inside the append's own transaction.
        if not control_items.consume(tx.conn, items, tx.fold.seq + 1):
            return Refusal(None)
        return drafts

    done = await rt.append_decided(decide)
    if isinstance(done, Refusal):
        return None
    return lost(done.error) if isinstance(done, Err) else None


def _applied(tx: DecideTx, item: control_items.Item) -> Sequence[Draft]:
    """What one item appends: a cancel's barrier with the parks it releases, or the soft stop."""
    if item.command == "cancel":
        return barred(tx.fold, thread_barrier(item.principal))
    return (soft_stop(item.principal),)
