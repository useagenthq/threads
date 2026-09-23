"""Rewrites a stored log the way a writer (or an import) would have produced it: every line
canonical, re-chained, the head moved, and thread_started's config_hash recomputed when its config
changed. The result verifies, so a test gets a real log with the shape it needs."""

import copy
import sqlite3
from collections.abc import Callable, Sequence

from pydantic import JsonValue, TypeAdapter

from threads import Store
from threads.agents.store import open_store
from threads.log import ThreadId
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.result import Ok

type Line = dict[str, JsonValue]
_LINE: TypeAdapter[Line] = TypeAdapter(Line)


def obj(value: JsonValue) -> Line:
    """A JSON object of a log line."""
    return _LINE.validate_python(value)


def config_hash(config: Line) -> str:
    """config_hash of an agent with no extensions, skills, memory, sandbox or subagents: its
    canonical pin."""
    text = canonicalize(config)
    assert isinstance(text, Ok)
    return sha256_hex(text.value.encode())


def _config(line: Line) -> Line:
    """The pin config_hash covers: thread_started's data but its hash and its parent link."""
    return {k: v for k, v in obj(line["data"]).items() if k not in ("config_hash", "parent")}


def edit_started(edit: Callable[[Line], Line]) -> Callable[[list[Line]], list[Line]]:
    """An edit that replaces thread_started's data with `edit(data)`."""

    def apply(lines: list[Line]) -> list[Line]:
        started, *rest = lines
        return [{**started, "data": edit(obj(started["data"]))}, *rest]

    return apply


async def rewrite_log(
    store: Store, thread: ThreadId, edit: Callable[[list[Line]], list[Line]]
) -> None:
    """Replaces `thread`'s main-branch lines with `edit(lines)` (as many lines or fewer)."""
    sq = await open_store(store)
    root = await sq.root(thread)
    assert isinstance(root, Ok)
    branch = root.value

    def rewrite(c: sqlite3.Connection) -> None:
        rows: Sequence[tuple[int, bytes]] = c.execute(
            "SELECT seq, line FROM events WHERE branch_id = ? ORDER BY seq", (branch,)
        ).fetchall()
        (header,) = c.execute(
            "SELECT header_line FROM branches WHERE branch_id = ?", (branch,)
        ).fetchone()
        before = [_LINE.validate_json(line) for _, line in rows]
        after = edit(copy.deepcopy(before))
        if after and _config(after[0]) != _config(before[0]):
            # Proves the formula on the stored pin before re-hashing the edited one.
            assert obj(before[0]["data"])["config_hash"] == config_hash(_config(before[0]))
            data = {**obj(after[0]["data"]), "config_hash": config_hash(_config(after[0]))}
            after[0] = {**after[0], "data": data}
        prev = sha256_hex(header)
        for (seq, _), line in zip(rows, after, strict=False):
            text = canonicalize({**line, "prev_hash": prev})
            assert isinstance(text, Ok)
            raw = text.value.encode()
            c.execute(
                "UPDATE events SET line = ? WHERE branch_id = ? AND seq = ?", (raw, branch, seq)
            )
            prev = sha256_hex(raw)
        c.execute("DELETE FROM events WHERE branch_id = ? AND seq > ?", (branch, len(after)))
        c.execute(
            "UPDATE branches SET head_seq = ?, head_hash = ? WHERE branch_id = ?",
            (len(after), prev, branch),
        )

    await sq.run(rewrite)
