"""The op vectors (spec/conformance/vectors/team-ops.json) as a runtime reads them: a world's logs
seeded under this implementation's own headers and chained, its index rebuilt, every member
config the vectors pin stored under its config_hash, and the rows as the vectors list them."""

import json
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter
from team.team_kit import add, canonical, index_rows

from threads.log import BranchId, ThreadId
from threads.log.digest import sha256_hex
from threads.result import Ok
from threads.store import SqliteStore
from threads.store.conn import Conn
from threads.store.lines import header_line
from threads.team.rebuild import rebuild_team_index

FILE = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "team-ops.json"
type Obj = dict[str, JsonValue]
_OBJ: TypeAdapter[Obj] = TypeAdapter(Obj)
DOC: Final = _OBJ.validate_python(json.loads(FILE.read_text(encoding="utf-8")))
TEAM: Final = "0192c000-0000-7000-8000-000000000001"
KEYS: Final[dict[str, tuple[str, ...]]] = {
    "teams": ("team_id",),
    "team_members": ("team_id", "name", "generation"),
    "mail": ("mail_id",),
    "asks": ("ask_id",),
    "monitors": ("monitor_id",),
    "operator_receipts": ("tenant_id", "team_id", "op", "idempotency_key"),
    "pending_wakes": ("branch_id", "child_thread_id"),
}


def obj(value: JsonValue) -> Obj:
    return _OBJ.validate_python(value)


def vectors() -> list[Obj]:
    found = DOC["vectors"]
    assert isinstance(found, list)
    return [obj(v) for v in found]


def world_logs(v: Obj) -> dict[str, Obj]:
    world = obj(obj(DOC["worlds"])[str(obj(v["given"])["world"])])
    return {label: obj(log) for label, log in obj(world["logs"]).items()}


def _exported(ref: Obj) -> bytes:
    """One world log's export: this implementation's header, the events chained, the head."""
    header = header_line(ThreadId(str(ref["thread_id"])), BranchId(str(ref["branch_id"])), 0)
    header = canonical({**obj(json.loads(header)), "created_at": 1_790_000_000_000})
    lines, prev, seq = [header], sha256_hex(header), 0
    events = obj(DOC["events"])
    ids = ref["events"]
    assert isinstance(ids, list)
    for ident in ids:
        event = obj(events[str(ident)])
        line = canonical({**event, "prev_hash": prev})
        lines.append(line)
        prev, seq = sha256_hex(line), event["seq"]
    head = {"format": "threads.head", "format_version": 1, "branch_id": ref["branch_id"]}
    lines.append(canonical({**head, "seq": seq, "hash": prev}))
    return b"\n".join(lines) + b"\n"


async def seeded(v: Obj, path: str = ":memory:") -> SqliteStore:
    """A store holding the vector's world, its team index rebuilt, every pinned config stored."""
    opened = await SqliteStore.open(path, tenant_id="acme")
    assert isinstance(opened, Ok)
    store = opened.value
    await add(store, {label: _exported(ref) for label, ref in world_logs(v).items()}, "acme")
    assert await rebuild_team_index(store, TEAM) == Ok(None)
    for config in configs():
        await store.put_artifact(config)
    return store


def configs() -> list[bytes]:
    """The pinned config of every thread_started among the vectors' events."""
    out: list[bytes] = []
    for event in obj(DOC["events"]).values():
        e = obj(event)
        if e["type"] != "thread_started":
            continue
        data = obj(e["data"])
        cfg = {k: val for k, val in data.items() if k not in ("config_hash", "parent", "team")}
        raw = canonical(cfg)
        if sha256_hex(raw) == data["config_hash"]:
            out.append(raw)
    return out


def agents() -> dict[str, str]:
    """The agents the worlds' team lists, each with the config_hash its member logs pin."""
    out: dict[str, str] = {}
    for event in obj(DOC["events"]).values():
        e = obj(event)
        data = obj(e["data"])
        if e["type"] == "thread_started" and data["agent_name"] != "lead":
            out[str(data["agent_name"])] = str(data["config_hash"])
    return out


def rows(conn: Conn) -> Obj:
    found = index_rows(conn)
    return {t: found[t] for t in KEYS}


def _key(table: str, row: JsonValue) -> bytes:
    r = obj(row)
    return canonical({k: r.get(k) for k in KEYS[table]})


def changes(before: Obj, after: Obj) -> Obj:
    """Per table, the rows inserted, updated (as they are now) and the keys deleted."""
    out: Obj = {}
    for table, cols in KEYS.items():
        old_rows, new_rows = before[table], after[table]
        assert isinstance(old_rows, list)
        assert isinstance(new_rows, list)
        old = {_key(table, r): r for r in old_rows}
        new = {_key(table, r): r for r in new_rows}
        change: Obj = {}
        inserted = [r for k, r in new.items() if k not in old]
        updated = [r for k, r in new.items() if k in old and canonical(old[k]) != canonical(r)]
        deleted: list[JsonValue] = [
            {c: obj(r).get(c) for c in cols} for k, r in old.items() if k not in new
        ]
        for name, got in (("insert", inserted), ("update", updated), ("delete", deleted)):
            if got:
                change[name] = got
        if change:
            out[table] = change
    return out


def vector_mint(seq: int, _now: int) -> str:
    """The reference's event ids: `eid(seq, branch)` for a team branch."""
    return f"0192e001-0000-7000-8000-{seq:012x}"
