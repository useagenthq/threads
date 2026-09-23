"""The tree-wide budget ledger: one row per budget,
limit and model attempt. A reservation checks every covering budget and inserts its rows in one
transaction, so concurrent threads of a tree can't overspend together."""

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from threads.store.sql import transaction
from threads.store.worker import Worker

type LimitName = Literal[
    "max_cost_nanos", "max_input_tokens", "max_output_tokens", "max_model_requests"
]


@dataclass(frozen=True, slots=True)
class Cover:
    """One budget covering the attempt: `thread:<id>` or `run:<thread_id>:<user_input id>`."""

    budget_id: str
    limits: Mapping[LimitName, int]


@dataclass(frozen=True, slots=True)
class Refused:
    """The first limit the reservation would pass: settled plus reserved plus this one."""

    budget_id: str
    limit: LimitName
    limit_value: int
    observed: int


class BudgetLedger:
    def __init__(self, worker: Worker) -> None:
        self._worker = worker

    async def reserve(
        self, attempt_key: str, covers: Sequence[Cover], amounts: Mapping[LimitName, int]
    ) -> Refused | None:
        """Inserts `reserved` rows for every covering (budget, limit) only if all fit."""

        def run(conn: sqlite3.Connection) -> Refused | None:
            with transaction(conn):
                for cover in covers:
                    for limit, value in cover.limits.items():
                        spent = _sum(conn, cover.budget_id, limit)
                        observed = spent + amounts.get(limit, 0)
                        if observed > value:
                            return Refused(cover.budget_id, limit, value, observed)
                _insert(conn, attempt_key, covers, amounts, "reserved")
            return None

        return await self._worker.call(run)

    async def record(
        self, attempt_key: str, covers: Sequence[Cover], amounts: Mapping[LimitName, int]
    ) -> None:
        """Rebuild: an attempt already in the log, entered settled without a check."""

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                _insert(conn, attempt_key, covers, amounts, "settled")

        await self._worker.call(run)

    async def settle(self, attempt_key: str, amounts: Mapping[LimitName, int]) -> None:
        """Replaces the attempt's reservation with its disposition."""

        def run(conn: sqlite3.Connection) -> None:
            with transaction(conn):
                for limit, amount in amounts.items():
                    conn.execute(
                        "UPDATE budget_ledger SET amount = ?, state = 'settled'"
                        " WHERE attempt_key = ? AND limit_name = ?",
                        (amount, attempt_key, limit),
                    )

        await self._worker.call(run)

    async def release(self, attempt_key: str) -> None:
        """Drops a reservation whose attempt never reached the log."""

        def run(conn: sqlite3.Connection) -> None:
            conn.execute("DELETE FROM budget_ledger WHERE attempt_key = ?", (attempt_key,))

        await self._worker.call(run)

    async def attempts(self, branch_id: str) -> Mapping[str, bool]:
        """This branch's attempt keys, and whether each is still (partly) reserved."""

        def run(conn: sqlite3.Connection) -> dict[str, bool]:
            rows: list[tuple[str, int]] = conn.execute(
                "SELECT attempt_key, max(state = 'reserved') FROM budget_ledger"
                " WHERE substr(attempt_key, 1, ?) = ? GROUP BY attempt_key",
                (len(branch_id) + 1, f"{branch_id}:"),
            ).fetchall()
            return {key: bool(reserved) for key, reserved in rows}

        return await self._worker.call(run)


def _sum(conn: sqlite3.Connection, budget_id: str, limit: LimitName) -> int:
    row: tuple[int] = conn.execute(
        "SELECT coalesce(sum(amount), 0) FROM budget_ledger WHERE budget_id = ? AND limit_name = ?",
        (budget_id, limit),
    ).fetchone()
    return row[0]


def _insert(
    conn: sqlite3.Connection,
    attempt_key: str,
    covers: Sequence[Cover],
    amounts: Mapping[LimitName, int],
    state: Literal["reserved", "settled"],
) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            (cover.budget_id, limit, attempt_key, amounts.get(limit, 0), state)
            for cover in covers
            for limit in cover.limits
        ],
    )
