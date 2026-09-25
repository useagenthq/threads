"""The tree-wide budget ledger (store.sql `budget_ledger`): one row per budget,
limit and model attempt. A reservation checks every covering budget and inserts its rows in one
transaction, so concurrent threads of a tree can't overspend together."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from threads.store.conn import Conn, one
from threads.store.sql import int_of, text_of, transaction
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
        self, attempt_key: str, covers: Sequence[Cover], amounts: Mapping[LimitName, int | None]
    ) -> Refused | None:
        """`reserved` rows for every covering (budget, limit), only if all fit. An amount of None
        (no per-attempt bound) never fits: it is refused at what is settled. The same key again (a
        commit whose outcome was unknown) is checked against everything but its own rows, then
        replaces them, as TypeScript's does."""

        def run(conn: Conn) -> Refused | None:
            with transaction(conn):
                for cover in covers:
                    for limit, value in cover.limits.items():
                        spent = _sum(conn, cover.budget_id, limit, except_key=attempt_key)
                        amount = amounts.get(limit, 0)
                        if amount is None:
                            return Refused(cover.budget_id, limit, value, spent)
                        if spent + amount > value:
                            return Refused(cover.budget_id, limit, value, spent + amount)
                _insert(conn, attempt_key, covers, amounts, "reserved")
            return None

        return await self._worker.call(run)

    async def record(
        self, attempt_key: str, covers: Sequence[Cover], amounts: Mapping[LimitName, int]
    ) -> None:
        """Rebuild: an attempt already in the log, entered settled without a check."""

        def run(conn: Conn) -> None:
            with transaction(conn):
                _insert(conn, attempt_key, covers, amounts, "settled")

        await self._worker.call(run)

    async def settle(self, attempt_key: str, amounts: Mapping[LimitName, int]) -> None:
        """Replaces the attempt's reservation with its disposition."""

        def run(conn: Conn) -> None:
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

        def run(conn: Conn) -> None:
            conn.execute("DELETE FROM budget_ledger WHERE attempt_key = ?", (attempt_key,))

        await self._worker.call(run)

    async def spent(self, budget_id: str, limit: LimitName) -> int:
        """What a budget's limit has spent and holds reserved: its ledger rows' sum."""
        return await self._worker.read(lambda c: _sum(c, budget_id, limit))

    async def attempts(self, branch_id: str) -> Mapping[str, bool]:
        """This branch's attempt keys, and whether each is still (partly) reserved."""

        def run(conn: Conn) -> dict[str, bool]:
            rows = conn.execute(
                "SELECT attempt_key, MAX(CASE WHEN state = 'reserved' THEN 1 ELSE 0 END)"
                " FROM budget_ledger WHERE substr(attempt_key, 1, length(CAST(? AS TEXT))) = ?"
                " GROUP BY attempt_key",
                (f"{branch_id}:", f"{branch_id}:"),
            ).fetchall()
            return {text_of(key): int_of(reserved) == 1 for key, reserved in rows}

        return await self._worker.read(run)


_INSERT = (
    "INSERT INTO budget_ledger (budget_id, limit_name, attempt_key, amount, state)"
    " VALUES (?, ?, ?, ?, ?)"
)
_RESERVE: Final = (
    _INSERT + " ON CONFLICT (budget_id, limit_name, attempt_key)"
    " DO UPDATE SET amount = excluded.amount, state = 'reserved'"
)
_RECORD: Final = _INSERT + " ON CONFLICT DO NOTHING"


def _sum(conn: Conn, budget_id: str, limit: LimitName, *, except_key: str = "") -> int:
    row = conn.execute(
        "SELECT CAST(COALESCE(SUM(amount), 0) AS BIGINT) FROM budget_ledger"
        " WHERE budget_id = ? AND limit_name = ? AND attempt_key <> ?",
        (budget_id, limit, except_key),
    ).fetchone()
    return int_of(one(row)[0])


def _insert(
    conn: Conn,
    attempt_key: str,
    covers: Sequence[Cover],
    amounts: Mapping[LimitName, int | None],
    state: Literal["reserved", "settled"],
) -> None:
    # A reservation replaces its key's rows; a rebuild's settled row never overwrites one.
    conn.executemany(
        _RESERVE if state == "reserved" else _RECORD,
        [
            (cover.budget_id, limit, attempt_key, amounts.get(limit, 0) or 0, state)
            for cover in covers
            for limit in cover.limits
        ],
    )
