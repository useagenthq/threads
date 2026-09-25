"""Test-only Postgres connections for the lane 27 drills: one that forces serialization failures
at chosen statements, and one whose COMMIT loses its connection."""

from collections.abc import Callable, Iterable
from typing import Literal

from pg_kit import Leg, admin, schema_url

from threads.postgres.driver import PgConn, raw_connector
from threads.postgres.opening import open_postgres
from threads.result import Ok
from threads.store import SqliteStore
from threads.store.conn import CommitUnknownError, Cursor, Params, RetryableError


class DrillConn(PgConn):
    def __init__(self, url: str) -> None:
        super().__init__(raw_connector(url))
        self.url = url


class Forcing(DrillConn):
    """Raises 40001 at the next `times` statements `at` matches, as a concurrent transaction
    would; the statement itself never runs."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.at: Callable[[str], bool] = lambda _sql: False
        self.times = 0

    def force(self, at: Callable[[str], bool], times: int = 1) -> None:
        self.at, self.times = at, times

    def _execute(self, sql: str, params: Params) -> Cursor:
        if self.times and self.at(sql):
            self.times -= 1
            raise RetryableError("forced 40001")
        return super()._execute(sql, params)

    def _executemany(self, sql: str, rows: Iterable[Params]) -> None:
        if self.times and self.at(sql):
            self.times -= 1
            raise RetryableError("forced 40001")
        super()._executemany(sql, rows)

    def commit(self) -> None:
        if self.times and self.at("COMMIT"):
            self.times -= 1
            self.raw.execute("ROLLBACK")
            raise RetryableError("forced 40001 at COMMIT")
        super().commit()


class Dropping(DrillConn):
    """The next COMMIT's connection is lost: `before` the server commits (its backend is
    terminated, so the real driver path reports it), or `after` (the reply is lost)."""

    def __init__(self, url: str) -> None:
        super().__init__(url)
        self.drop: Literal["before", "after"] | None = None

    def commit(self) -> None:
        drop, self.drop = self.drop, None
        if drop == "before":
            row = self.raw.execute("SELECT pg_backend_pid()").fetchone()
            with admin(self.url) as other:
                other.execute("SELECT pg_terminate_backend(%s)", (None if row is None else row[0],))
        super().commit()
        if drop == "after":
            self.raw.close()
            raise CommitUnknownError("the connection dropped after COMMIT was sent")


async def opened_with[C: DrillConn](
    leg: Leg, make: Callable[[str], C], url: str | None = None, tenant: str = "local"
) -> tuple[SqliteStore, C]:
    """A store on a fresh schema (or on `url`) whose connection `make` builds."""
    target = url or schema_url(leg.url, leg.schema())
    made: list[C] = []

    def connect() -> C:
        conn = make(target)
        made.append(conn)
        leg.conns.append(conn)
        return conn

    store = await open_postgres(target, tenant, connect=connect)
    assert isinstance(store, Ok)
    return store.value, made[0]
