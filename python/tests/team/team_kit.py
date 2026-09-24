"""Team logs in a store, for the tests of the team index, rule 43, tree walks and deletion.

The staged team cases (spec/conformance/staged) are the fixtures: real logs of the Teams
contract. Until lane 21A implements rules 31-45, every reader refuses a team event with
unsupported_critical_event, so `lift_refusal` lifts that pre-build refusal for one test. Remove
it when lane 21A replaces the pre-build refusal.
"""

import copy
import json
import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter

from threads.agents.store import Store, open_store
from threads.log import BranchId, ParseError, ThreadId
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.reduce import rules_team
from threads.result import Err, Ok
from threads.store import SqliteStore, VerifiedLog, sql, verify_export
from threads.team.rebuild import TEAM_TABLES

STAGED = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "staged"
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


def lift_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readers reduce team logs (rules 31-45 unchecked) for the rest of the test."""
    monkeypatch.setattr(rules_team, "not_yet", lambda _event: None)


def team_cases() -> list[str]:
    return sorted(
        d.name
        for d in STAGED.iterdir()
        if json.loads((d / "case.json").read_text())["kind"] == "team"
    )


def staged(case: str) -> dict[str, bytes]:
    """The case's logs by label."""
    return {p.stem: p.read_bytes() for p in sorted((STAGED / case / "logs").glob("*.jsonl"))}


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
    """A fresh store holding the logs byte for byte (no request replay: the staged cases ship no
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


def index_rows(conn: sqlite3.Connection, team_id: str) -> dict[str, JsonValue]:
    """The team's rows as the conformance `index` holds them: each table by primary key, JSON
    columns parsed, mail's claim columns left out, the feed as (branch_id, seq)."""
    out: dict[str, JsonValue] = {t: _rows(conn, t, team_id) for t in TEAM_TABLES}
    feed = conn.execute(
        "SELECT branch_id, seq FROM team_feed WHERE team_id = ? ORDER BY branch_id, seq",
        (team_id,),
    ).fetchall()
    out["team_feed"] = [{"branch_id": b, "seq": s} for b, s in feed]
    wakes = conn.execute(
        "SELECT branch_id, child_thread_id FROM pending_wakes ORDER BY branch_id, child_thread_id"
    ).fetchall()
    out["pending_wakes"] = [{"branch_id": b, "child_thread_id": c} for b, c in wakes]
    return out


def _rows(conn: sqlite3.Connection, table: str, team_id: str) -> JsonValue:
    cursor = conn.execute(
        f"SELECT * FROM {table} WHERE team_id = ? ORDER BY {_PKS[table]}",  # noqa: S608
        (team_id,),
    )
    names = [d[0] for d in cursor.description]
    rows: list[JsonValue] = []
    for values in cursor.fetchall():
        row = {n: _cell(n, v) for n, v in zip(names, values, strict=True)}
        rows.append({n: v for n, v in row.items() if n not in ("claim_token", "claim_expires_at")})
    return rows


def _cell(name: str, value: object) -> JsonValue:
    if name in _JSON_COLUMNS and isinstance(value, bytes):
        return _JSON.validate_json(value)
    return _JSON.validate_python(value)


def branch_of(thread: ThreadId) -> BranchId:
    """The staged fixtures number a thread's branch like the thread."""
    return BranchId(thread.replace("0192a000", "0192b000"))
