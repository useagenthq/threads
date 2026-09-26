# pyright: strict
"""`a2a.json`: the five A2A tables both runtimes read their own implementation over, which is what
proves the two agree about A2A 1.0 without either reading the other's code.

Every expected value comes from the pinned protocol, never from memory: the error codes are parsed
out of the table in `spec/schema/a2a/README.md` (itself copied from the specification's §5.4
mapping), and the rest is the behaviour the protocol source states —
`typescript/packages/a2a/src/protocol/` and `python/src/threads/a2a/protocol/`.

- `errors`: every A2A error name with its JSON-RPC code and HTTP status.
- `task_states`: the eight states we accept, each with `terminal` and `interrupted`, plus the two
  kinds of name that must not parse (the proto's zero value, and one nobody defined).
- `versions`: the `A2A-Version` header and query parameter, and whether the request is answered.
- `paths`: the inbound HTTP+JSON path table, with the task id each path carries.
- `sse`: a body and the events a reader dispatches from it.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from .common import CASES
from .pieces import dump

if TYPE_CHECKING:
    from .jcs import JsonValue

VECTOR = CASES.parent / "vectors" / "a2a.json"
README = CASES.parents[1] / "schema" / "a2a" / "README.md"

# `| JSONParseError | -32700 | 400 |` in the README's error-code table. Reading the table rather
# than repeating it means a vector row can never disagree with the document it came from.
ERROR_ROW = re.compile(r"^\| `(\w+Error)` \| `(-?\d+)` \| (\d{3}) \|$", re.MULTILINE)
ERRORS_EXPECTED = 14

# The eight states we accept, and the two groups the pinned spec calls terminal and interrupted.
TASK_STATES = (
    "TASK_STATE_SUBMITTED",
    "TASK_STATE_WORKING",
    "TASK_STATE_COMPLETED",
    "TASK_STATE_FAILED",
    "TASK_STATE_CANCELED",
    "TASK_STATE_INPUT_REQUIRED",
    "TASK_STATE_REJECTED",
    "TASK_STATE_AUTH_REQUIRED",
)
TERMINAL = frozenset(
    {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
)
INTERRUPTED = frozenset({"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"})

# `{header, query, accepted}`. Only Major.Minor is compared, so 1.0.1 is 1.0; both absent is 0.3
# by the specification's own rule, which is VersionNotSupportedError rather than a default to 1.0.
VERSIONS: tuple[tuple[str, str | None, str | None, bool], ...] = (
    ("the header we send", "1.0", None, True),
    ("the query parameter instead, which the spec allows", None, "1.0", True),
    ("neither, which the spec reads as 0.3", None, None, False),
    ("a 0.3 peer", "0.3", None, False),
    ("a patch version, since only Major.Minor is compared", "1.0.1", None, True),
    ("the next major", "2.0", None, False),
    ("surrounding whitespace", "  1.0\t", None, True),
)

# `{path, method, id}` for the inbound HTTP+JSON table. A task id is percent-decoded, so a path
# segment can carry a slash; `method: null` is a path that is not one of ours at all.
PATHS: tuple[tuple[str, str, str | None, str | None], ...] = (
    ("send", "/message:send", "SendMessage", None),
    ("stream", "/message:stream", "SendStreamingMessage", None),
    ("list", "/tasks", "ListTasks", None),
    ("get one task", "/tasks/abc", "GetTask", "abc"),
    ("cancel", "/tasks/abc:cancel", "CancelTask", "abc"),
    ("subscribe", "/tasks/abc:subscribe", "SubscribeToTask", "abc"),
    ("a percent-escaped slash in the task id", "/tasks/a%2Fb", "GetTask", "a/b"),
    (
        "a percent-escaped colon, so the suffix is part of the id",
        "/tasks/a%3Acancel",
        "GetTask",
        "a:cancel",
    ),
    ("the root", "/", None, None),
    ("a task path with no id", "/tasks/", None, None),
    ("two id segments", "/tasks/a/b", None, None),
    ("something after a verb", "/message:send/extra", None, None),
    ("a verb we do not serve", "/message:resume", None, None),
)

# `{raw, events}` for the reader. The WHATWG event-stream rules: a `\r\n`, `\n` or `\r` line break;
# a blank line dispatches; one leading space after the colon is dropped; several `data:` lines join
# with `\n`; a `:` comment is ignored; an `id:` stays set for the events after it; a block that was
# never terminated was never dispatched.
SSE: tuple[tuple[str, str, tuple[tuple[str | None, str], ...]], ...] = (
    ("one data line", "data: hello\n\n", ((None, "hello"),)),
    ("several data lines join with a newline", "data: a\ndata: b\n\n", ((None, "a\nb"),)),
    ("CRLF breaks", "data: a\r\n\r\n", ((None, "a"),)),
    ("a bare CR breaks", "data: a\r\r", ((None, "a"),)),
    ("a comment is ignored", ":ping\ndata: a\n\n", ((None, "a"),)),
    (
        "an id carries to the events after it",
        "id: 7\ndata: a\n\ndata: b\n\n",
        (("7", "a"), ("7", "b")),
    ),
    ("an unterminated block is dropped", "data: a\n\ndata: b\n", ((None, "a"),)),
)


def _errors() -> list[JsonValue]:
    """The error table as the README writes it, which is the specification's §5.4 mapping."""
    rows = ERROR_ROW.findall(README.read_text(encoding="utf-8"))
    if len(rows) != ERRORS_EXPECTED:
        raise AssertionError(
            f"{README.name}: found {len(rows)} error rows, expected {ERRORS_EXPECTED}; "
            f"the table's shape changed, so a2a_vectors.py needs re-reading, not a new number"
        )
    # The row's name is the error's name: it is what the row pins.
    return [{"name": name, "code": int(code), "status": int(status)} for name, code, status in rows]


def _task_states() -> list[JsonValue]:
    rows: list[JsonValue] = [
        {
            "name": state.removeprefix("TASK_STATE_").lower(),
            "state": state,
            "accepted": True,
            "terminal": state in TERMINAL,
            "interrupted": state in INTERRUPTED,
        }
        for state in TASK_STATES
    ]
    rows.append(
        {
            "name": "the proto's zero value, which is never an answer we can act on",
            "state": "TASK_STATE_UNSPECIFIED",
            "accepted": False,
        }
    )
    rows.append(
        {"name": "a state nobody defined", "state": "TASK_STATE_PONDERING", "accepted": False}
    )
    return rows


def _versions() -> list[JsonValue]:
    return [{"name": n, "header": h, "query": q, "accepted": ok} for n, h, q, ok in VERSIONS]


def _paths() -> list[JsonValue]:
    return [{"name": n, "path": p, "method": m, "id": i} for n, p, m, i in PATHS]


def _sse() -> list[JsonValue]:
    return [
        {"name": n, "raw": raw, "events": [{"id": i, "data": d} for i, d in events]}
        for n, raw, events in SSE
    ]


def _tables() -> JsonValue:
    return {
        "errors": _errors(),
        "task_states": _task_states(),
        "versions": _versions(),
        "paths": _paths(),
        "sse": _sse(),
    }


def write() -> None:
    VECTOR.write_text(dump(_tables()))


def check() -> list[str]:
    if not VECTOR.is_file() or VECTOR.read_text() != dump(_tables()):
        return [f"{VECTOR.name} is stale; run gen_fixtures.py"]
    return []
