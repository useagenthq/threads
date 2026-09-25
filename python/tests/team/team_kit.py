"""Team logs in a store, for the tests of the team index, rule 43, tree walks and deletion.

The corpus team cases (spec/conformance/cases, kind team) are the fixtures: real logs of the
Teams contract.
"""

import copy
import json
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.store import Store, open_store
from threads.log import (
    AskClosedEvent,
    BranchId,
    Event,
    MailRefusedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    OperatorRequestEvent,
    ParseError,
    ThreadId,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog, sql, verify_export
from threads.store.conn import Conn
from threads.team.cross import TeamLogEvents, check_team_logs
from threads.team.rebuild import rebuild_team_index, team_branches

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
TEAM = "0192c000-0000-7000-8000-000000000001"
TENANT = "acme"
LEAD = ThreadId("0192a000-0000-7000-8000-0000000000b1")
MEMBER = ThreadId("0192a000-0000-7000-8000-0000000000b2")
TEAM_LOG = ThreadId("0192a000-0000-7000-8000-0000000000b3")

type Line = dict[str, JsonValue]
type Edit = Callable[[list[Line]], list[Line]]
_LINE: TypeAdapter[Line] = TypeAdapter(Line)
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_JSON_COLUMNS = frozenset({"provenance", "result", "envelope"})
_PKS: Mapping[str, str] = {
    "teams": "team_id",
    "team_members": "team_id, name, generation",
    "mail": "mail_id",
    "asks": "ask_id",
    "monitors": "monitor_id",
    "operator_receipts": "tenant_id, team_id, op, idempotency_key",
}
_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "teams": ("team_id", "tenant_id", "lead_thread_id", "team_log_branch_id", "closed_at"),
    "team_members": (
        *("team_id", "name", "generation", "role", "agent", "config_hash", "thread_id"),
        *("branch_id", "provenance", "state", "result", "updated_seq"),
    ),
    "mail": (
        *("mail_id", "team_id", "kind", "to_name", "to_generation", "principal_key"),
        *("root_request", "envelope", "created_at", "state", "consumed_seq"),
    ),
    "asks": (
        *("ask_id", "team_id", "asker_branch_id", "recipient_name", "recipient_generation"),
        *("deadline", "state", "closed_seq"),
    ),
    "monitors": (
        *("monitor_id", "team_id", "watcher_branch_id", "target_name", "target_generation"),
        *("kind", "wait_id"),
    ),
    "operator_receipts": (
        *("tenant_id", "team_id", "op", "idempotency_key", "principal_key", "body_hash"),
        "request_id",
    ),
}
"""Each table's columns as store.sql names them; mail's claim columns, wake hints, left out."""


def team_cases() -> list[str]:
    return sorted(
        d.name
        for d in CASES.iterdir()
        if json.loads((d / "case.json").read_text())["kind"] == "team"
    )


def case_logs(case: str) -> dict[str, bytes]:
    """The case's logs by label."""
    return {p.stem: p.read_bytes() for p in sorted((CASES / case / "logs").glob("*.jsonl"))}


def canonical(value: JsonValue) -> bytes:
    text = canonicalize(value)
    assert isinstance(text, Ok)
    return text.value.encode()


def rechain(raw: bytes, edit: Edit) -> bytes:
    """The log with its events replaced by `edit(events)`, re-chained and headed as a writer
    would have written it."""
    header, *rest = raw.splitlines()
    events = [_LINE.validate_json(line) for line in rest if b'"prev_hash"' in line]
    prev, out = sha256_hex(header), [header]
    for event in edit(copy.deepcopy(events)):
        line = canonical({**event, "prev_hash": prev})
        out.append(line)
        prev = sha256_hex(line)
    branch = _LINE.validate_json(header)["branch_id"]
    head = {"format": "threads.head", "format_version": 1, "branch_id": branch}
    out.append(canonical({**head, "seq": len(out) - 1, "hash": prev}))
    return b"\n".join(out) + b"\n"


def verified(raw: bytes) -> Ok[VerifiedLog] | Err[ParseError]:
    return verify_export(raw, 0)


async def stored(logs: Mapping[str, bytes], tenant: str = TENANT) -> SqliteStore:
    """A fresh store holding the logs byte for byte (no request replay: the team cases ship no
    artifacts), with empty team tables."""
    return await open_store(await holding(logs, tenant))


async def holding(logs: Mapping[str, bytes], tenant: str = TENANT) -> Store:
    """`stored`, as the public Store handle the thread API takes."""
    handle = Store(":memory:", tenant=tenant)
    store = await open_store(handle)
    await add(store, logs, tenant)
    return handle


async def add(store: SqliteStore, logs: Mapping[str, bytes], tenant: str = TENANT) -> None:
    for label, raw in logs.items():
        log = verified(raw)
        assert isinstance(log, Ok), (label, log)
        found = await store.run(lambda c, v=log.value: sql.import_segments(c, v, tenant, None))
        assert found is None, (label, found)


def index_rows(conn: Conn) -> dict[str, JsonValue]:
    """Every team's rows as the conformance `index` holds them: each table by primary key, JSON
    columns parsed, mail's claim columns left out, the feed as the (team_id, branch_id, seq) it
    holds."""
    out: dict[str, JsonValue] = {t: _rows(conn, t) for t in _PKS}
    feed = conn.execute(
        "SELECT team_id, branch_id, seq FROM team_feed ORDER BY team_id, branch_id, seq"
    ).fetchall()
    out["team_feed"] = [
        {"team_id": _cell("", t), "branch_id": _cell("", b), "seq": _cell("", s)}
        for t, b, s in feed
    ]
    wakes = conn.execute(
        "SELECT branch_id, child_thread_id FROM pending_wakes ORDER BY branch_id, child_thread_id"
    ).fetchall()
    out["pending_wakes"] = [
        {"branch_id": _cell("", b), "child_thread_id": _cell("", c)} for b, c in wakes
    ]
    return out


def _rows(conn: Conn, table: str) -> JsonValue:
    names = _COLUMNS[table]
    found = conn.execute(
        f"SELECT {', '.join(names)} FROM {table} ORDER BY {_PKS[table]}"  # noqa: S608
    ).fetchall()
    return [{n: _cell(n, v) for n, v in zip(names, values, strict=True)} for values in found]


async def rebuild_all(store: SqliteStore, logs: Mapping[str, bytes]) -> None:
    """Rebuilds every team a lead among the logs names."""
    for team in teams_of(logs):
        found = await rebuild_team_index(store, team)
        assert found == Ok(None), found


def teams_of(logs: Mapping[str, bytes]) -> dict[str, str]:
    """Every team a lead among the logs names, with that lead's label."""
    out: dict[str, str] = {}
    for label, raw in logs.items():
        read = verified(raw)
        assert isinstance(read, Ok), label
        started = next(
            (e for e in read.value.fold.events if isinstance(e, ThreadStartedEvent)), None
        )
        if started is not None and started.data.team is not MISSING:
            out[started.data.team.id] = label
    return out


def _cell(name: str, value: object) -> JsonValue:
    if name in _JSON_COLUMNS and isinstance(value, bytes):
        return _JSON.validate_json(value)
    return _JSON.validate_python(value)


def branch_of(thread: ThreadId) -> BranchId:
    """The team cases number a thread's branch like the thread."""
    return BranchId(thread.replace("0192a000", "0192b000"))


def appendable(conn: Conn, e: Event) -> bool:
    """Whether `e`'s append could have happened yet: the rows it moves exist (causal order)."""
    if isinstance(e, MessageReceivedEvent | UserInputEvent | MailRefusedEvent):
        mail = e.data.mail_id
        return mail is MISSING or _exists(conn, "SELECT 1 FROM mail WHERE mail_id = ?", mail)
    if isinstance(e, ThreadStartedEvent) and e.data.parent is not MISSING:
        return _exists(conn, "SELECT 1 FROM team_members WHERE thread_id = ?", e.thread_id)
    if isinstance(e, MessageSentEvent) and e.data.envelope.monitor_id is not MISSING:
        monitor = e.data.envelope.monitor_id
        return _exists(conn, "SELECT 1 FROM monitors WHERE monitor_id = ?", monitor)
    if isinstance(e, AskClosedEvent):
        return _exists(conn, "SELECT 1 FROM asks WHERE ask_id = ?", e.data.ask_id)
    if isinstance(e, OperatorRequestEvent):
        return _exists(conn, "SELECT 1 FROM teams WHERE team_log_branch_id = ?", e.branch_id)
    return True


def _exists(conn: Conn, query: str, key: object) -> bool:
    return conn.execute(query, (key,)).fetchone() is not None


async def assert_team_replays(store: SqliteStore, team: str) -> None:
    """The replay rule, checked on a live store: every log of the team verifies, rule 43 holds
    across them, and wiping and rebuilding the index leaves the rows the appends wrote, byte
    for byte (mail's claim columns aside; the feed compared as its (branch_id, seq) rows)."""
    tenant = store.tables.tenant_id

    def logs(conn: Conn) -> list[TeamLogEvents]:
        out: list[TeamLogEvents] = []
        for log in team_branches(conn, tenant, team):
            read = verify_export(sql.export(conn, log.branch_id), 0)
            assert isinstance(read, Ok), (log, read)
            out.append(TeamLogEvents(log.thread_id, log.branch_id, read.value.fold.events))
        return out

    assert check_team_logs(await store.run(logs), team) is None
    live = await store.run(index_rows)
    assert await rebuild_team_index(store, team) == Ok(None)
    assert await store.run(index_rows) == live
