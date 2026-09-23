"""Host-table statements bound to one append: an idempotency receipt, an
approval's consumption, an inbox item's consumption. They run in the append's transaction after
its rows, and a refusal rolls the whole append back, so the log and the host tables never
disagree."""

import sqlite3
from collections.abc import Callable, Sequence

from threads.log import ParseError
from threads.store.verify import StoredEvent

type Companion = Callable[[sqlite3.Connection, Sequence[StoredEvent]], ParseError | None]
