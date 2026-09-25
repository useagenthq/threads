"""`postgres()` (spec/api.json): the log and artifact store on Postgres 16 or later, for hosts on
several processes or machines. Install with the `postgres` extra (psycopg 3).

    from threads.postgres import postgres

    app = host(store=postgres(), agents={"assistant": assistant}, authenticate=authenticate)
"""

import os

from threads.agents.config import ConfigError
from threads.agents.store import Store
from threads.log import ParseError
from threads.result import Err, Ok
from threads.store import SqliteStore

__all__ = ["postgres"]


def postgres(url: str | None = None) -> Store:
    """spec/api.json `postgres`: a postgres:// URL, or DATABASE_URL. Nothing connects until
    first use; with neither, that use is invalid_config naming DATABASE_URL."""

    async def opener(tenant: str) -> Ok[SqliteStore] | Err[ParseError]:
        found = url if url is not None else os.environ.get("DATABASE_URL")
        if not found:
            raise ConfigError(
                "invalid_config", "postgres() needs a URL: pass one or set DATABASE_URL"
            )
        # psycopg is an optional extra: imported on first use, never by threads itself.
        from threads.postgres.opening import open_postgres  # noqa: PLC0415

        return await open_postgres(found, tenant)

    return Store("postgres", opener=opener)
