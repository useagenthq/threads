"""Host-table statements bound to one append: an idempotency receipt, an
approval's consumption, an inbox item's consumption. They run in the append's transaction after
its rows, and a refusal rolls the whole append back, so the log and the host tables never
disagree."""

from collections.abc import Callable, Sequence

from threads.log import ParseError
from threads.store.conn import Conn
from threads.store.verify import StoredEvent

type Companion = Callable[[Conn, Sequence[StoredEvent]], ParseError | None]


def both(first: Companion, then: Companion | None) -> Companion:
    """Two companions in one append's transaction; the first refusal wins."""
    if then is None:
        return first

    def run(conn: Conn, events: Sequence[StoredEvent]) -> ParseError | None:
        return first(conn, events) or then(conn, events)

    return run
