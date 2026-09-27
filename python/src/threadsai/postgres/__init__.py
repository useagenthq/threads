"""`postgres()` (spec/api.json): the log and artifact store on Postgres 16 or later, for hosts on
several processes or machines. Install with the `postgres` extra (psycopg 3).

    from threadsai.postgres import postgres

    app = host(store=postgres(), agents={"assistant": assistant}, authenticate=authenticate)
"""

import os

from threadsai.agents.config import ConfigError
from threadsai.agents.store import Store
from threadsai.log import ParseError
from threadsai.result import Err, Ok
from threadsai.store import SqliteStore

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
        from threadsai.postgres.opening import open_postgres  # noqa: PLC0415

        return await open_postgres(found, tenant)

    return Store("postgres", opener=opener)
