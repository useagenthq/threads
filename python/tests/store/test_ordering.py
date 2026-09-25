"""On both engines, text orders by its bytes (SQLite's BINARY, Postgres COLLATE "C"): member
names in mixed case, with non-ASCII and with digits list in the same order everywhere."""

from store.test_writer import run

from threads.store import SqliteStore
from threads.store.conn import Conn
from threads.team.rows import member_rows

NAMES = ("Zed", "alpha", "émile", "a-10", "a-9")


def _seed(conn: Conn) -> None:
    for i, name in enumerate(NAMES):
        conn.execute(
            "INSERT INTO team_members (team_id, name, generation, role, agent, config_hash,"
            " thread_id, branch_id, provenance, state, result, updated_seq)"
            " VALUES ('t', ?, 1, 'member', 'a', 'h', ?, ?, ?, 'idle', NULL, 1)",
            (name, f"thread-{i}", f"branch-{i}", b"{}"),
        )


def test_member_names_list_in_byte_order() -> None:
    async def test(store: SqliteStore) -> None:
        await store.run(_seed)
        rows = await store.run(lambda c: member_rows(c, "t"), read_only=True)
        assert [r.name for r in rows] == sorted(NAMES, key=lambda n: n.encode())
        assert [r.name for r in rows] == ["Zed", "a-10", "a-9", "alpha", "émile"]

    run(test)
