"""The `team` conformance kind (spec/conformance/README.md): import and reduce every log, check
rule 43 across them, fold the index into a fresh store from the logs alone, then walk the lead's
tree. It runs every corpus team case in full."""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING
from team.team_kit import (
    CASES,
    deleted_callers,
    holding,
    index_rows,
    team_cases,
    tombstone,
    verified,
)

from threads.agents.store import Store, open_store
from threads.agents.team_feed import InvalidCursorError, team_events
from threads.agents.team_handle_types import (
    EpochRestarted,
    MemberSource,
    OperatorSource,
    TeamCursor,
    TeamItem,
    TeamSource,
)
from threads.log import ParseError, TeamOpenedEvent, ThreadStartedEvent
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import LOCAL_TENANT, SqliteStore, VerifiedLog
from threads.store.sql import int_of, text_of
from threads.team.cross import TeamLogEvents, check_team_logs
from threads.team.index import opened_tenant as _opened_tenant
from threads.team.members import PENDING, open_member, team_members
from threads.team.rebuild import rebuild_team_index

_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
type Found = dict[str, JsonValue]


def _load(path: Path) -> dict[str, JsonValue]:
    value = _JSON.validate_json(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _failure(error: ParseError, log: str | None) -> Found:
    out: Found = {"code": error.code}
    if error.seq is not None:
        out["seq"] = error.seq
    if log is not None:
        out["log"] = log
    return out


def _leads(logs: dict[str, VerifiedLog]) -> dict[str, tuple[str, VerifiedLog]]:
    """Every team a lead among the logs names, with that lead's label and log."""
    out: dict[str, tuple[str, VerifiedLog]] = {}
    for label, log in logs.items():
        started = next((e for e in log.fold.events if isinstance(e, ThreadStartedEvent)), None)
        if started is not None and started.data.team is not MISSING:
            out[started.data.team.id] = (label, log)
    return out


def _teams(logs: dict[str, VerifiedLog]) -> dict[str, None]:
    """Every team the case's logs name, in log order: a lead's, and a leadless host team's."""
    out: dict[str, None] = {}
    for log in logs.values():
        first = next(iter(log.fold.events), None)
        if isinstance(first, TeamOpenedEvent) and first.data.kind == "host":
            out[first.data.team] = None
    return {**dict.fromkeys(_leads(logs)), **out}


def _tenant(logs: dict[str, VerifiedLog]) -> str:
    """The team's tenant, as its team_opened records it."""
    opened = (e for log in logs.values() for e in log.fold.events)
    return next((_opened_tenant(e) for e in opened if isinstance(e, TeamOpenedEvent)), LOCAL_TENANT)


def _imported(case: Path) -> tuple[dict[str, bytes], dict[str, VerifiedLog]] | Found:
    """Step 1: every log of input.logs read-only, in order; the first failure is the result."""
    inputs = _load(case / "case.json")["input"]
    assert isinstance(inputs, dict)
    labels_in = inputs["logs"]
    assert isinstance(labels_in, list)
    raw = {str(label): (case / "logs" / f"{label}.jsonl").read_bytes() for label in labels_in}
    logs: dict[str, VerifiedLog] = {}
    for label, data in raw.items():
        read = verified(data)
        if isinstance(read, Err):
            return _failure(read.error, label)
        logs[label] = read.value
    return raw, logs


async def run(case: Path) -> Found:
    """The case's outcome: {states, index, tree, feed}, or its first failure."""
    imported = _imported(case)
    if isinstance(imported, dict):
        return imported
    raw, logs = imported
    labels = {log.segments[-1].header.branch_id: label for label, log in logs.items()}
    broken = check_team_logs(
        [
            TeamLogEvents(h.thread_id, h.branch_id, log.fold.events)
            for log in logs.values()
            for h in (log.segments[-1].header,)
        ]
    )
    if broken is not None:
        return {"code": "invalid_transition", "seq": broken.seq, "log": labels[broken.branch_id]}
    states: Found = {label: log.state.to_json() for label, log in logs.items()}
    store = await holding(raw, _tenant(logs))
    sq = await open_store(store)
    await tombstone(sq, deleted_callers(case))
    leads = _leads(logs)
    for team in _teams(logs):
        rebuilt = await rebuild_team_index(sq, team)
        if isinstance(rebuilt, Err):
            return _failure(rebuilt.error, None)
    index = await sq.run(index_rows)
    tree = await _tree(store, leads)
    if "code" in tree:
        return tree
    found: Found = {"states": states, "index": index, "tree": tree}
    reads = _feed_reads(case)
    if reads is not None:
        found["feed"] = [await _feed(sq, next(iter(_teams(logs))), after) for after in reads]
    return found


def _feed_reads(case: Path) -> list[JsonValue] | None:
    """`input.feed`: one team.events read per entry, each `{}` or `{after: cursor}`."""
    inputs = _load(case / "case.json")["input"]
    assert isinstance(inputs, dict)
    reads = inputs.get("feed")
    if reads is None:
        return None
    assert isinstance(reads, list)
    return reads


async def _feed(sq: SqliteStore, team: str, read: JsonValue) -> JsonValue:
    """One team.events read over the rebuilt feed, as the `feed` projection records it."""
    assert isinstance(read, dict)
    cursor = read.get("after")
    after = None if cursor is None else TeamCursor(**_ints(cursor))
    try:
        items = [_item(i) for i in [x async for x in team_events(sq, team, after)]]
    except InvalidCursorError:
        return {"error": "invalid_cursor"}
    return {"items": items}


def _ints(cursor: JsonValue) -> dict[str, int]:
    assert isinstance(cursor, dict)
    return {k: v for k, v in cursor.items() if isinstance(v, int)}


def _item(item: TeamItem) -> JsonValue:
    cursor: JsonValue = {"epoch": item.cursor.epoch, "offset": item.cursor.offset}
    if isinstance(item, EpochRestarted):
        return {"kind": item.kind, "cursor": cursor}
    return {
        "kind": item.kind,
        "cursor": cursor,
        "source": _source(item.source),
        "branch_id": item.event.branch_id,
        "seq": item.event.seq,
    }


def _source(source: TeamSource) -> JsonValue:
    if isinstance(source, MemberSource):
        return {"kind": source.kind, "member": to_json(source.member)}
    if isinstance(source, OperatorSource):
        return {
            "kind": source.kind,
            "principal": to_json(source.principal),
            "request": source.request,
        }
    return {"kind": source.kind}


async def _tree(store: Store, leads: dict[str, tuple[str, VerifiedLog]]) -> Found:
    """Step 4: every team_members row in key order, each thread once, a lead row counted, a
    member row counted or pending as its own lead's walk finds it."""
    rows = await (await open_store(store)).run(
        lambda c: c.execute(
            "SELECT team_id, name, generation, role, thread_id FROM team_members"
            " ORDER BY team_id, name, generation"
        ).fetchall()
    )
    counted: list[JsonValue] = []
    pending: list[JsonValue] = []
    seen: set[str] = set()
    for columns in rows:
        team, name, role, thread = (text_of(columns[i]) for i in (0, 1, 3, 4))
        generation = int_of(columns[2])
        if thread in seen:  # a nested lead has two rows and one thread: counted once
            continue
        seen.add(thread)
        if role == "lead":  # where a walk starts: counted, never a child
            counted.append(name)
            continue
        if team not in leads:  # a host member: its leadless team has no tree to walk
            continue
        label, lead = leads[team]
        members = {(m.name, m.generation): m for m in await team_members(store, lead)}
        opened = await open_member(store, lead, members[(name, generation)])
        if isinstance(opened, Err):
            return _failure(opened.error, label)
        (pending if opened.value == PENDING else counted).append(name)
    return {"counted": counted, "pending": pending}


@pytest.mark.parametrize("name", team_cases())
def test_team_case(name: str) -> None:
    case = CASES / name
    expected = _load(case / "expected.json")
    got = asyncio.run(run(case))
    if expected["outcome"] == "error":
        assert got == expected["error"]
        return
    assert "code" not in got, got
    assert got["states"] == expected["states"]
    assert got["index"] == expected["index"]
    if "tree" in expected:  # a leadless host team has no tree to walk
        assert got["tree"] == expected["tree"]
    assert got.get("feed") == expected.get("feed")
