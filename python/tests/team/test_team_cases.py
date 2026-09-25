"""The `team` conformance kind (spec/conformance/README.md): import and reduce every log, check
rule 43 across them, fold the index into a fresh store from the logs alone, then walk the lead's
tree. It runs every corpus team case in full."""

import asyncio
from pathlib import Path

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING
from team.team_kit import CASES, holding, index_rows, team_cases, verified

from threads.agents.store import Store, open_store
from threads.log import ParseError, TeamOpenedEvent, ThreadStartedEvent
from threads.result import Err
from threads.store import LOCAL_TENANT, VerifiedLog
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
    """The case's outcome: {states, index, tree}, or its first failure."""
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
    leads = _leads(logs)
    for team in leads:
        rebuilt = await rebuild_team_index(sq, team)
        if isinstance(rebuilt, Err):
            return _failure(rebuilt.error, None)
    index = await sq.run(index_rows)
    tree = await _tree(store, leads)
    return tree if "code" in tree else {"states": states, "index": index, "tree": tree}


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
    assert got["tree"] == expected["tree"]
