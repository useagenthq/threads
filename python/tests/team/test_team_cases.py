"""The `team` conformance kind (spec/conformance/README.md): import and reduce every log, check
rule 43 across them, fold the index into a fresh store from the logs alone, then walk the lead's
tree. It runs every corpus team case, and every staged one with the pre-build refusal lifted and
without `states` (a mail-opened turn needs lane 21A's reducer), until 21A moves them."""

import asyncio
import json
from pathlib import Path

import pytest
from corpus import CASES
from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING
from team.team_kit import STAGED, holding, index_rows, lift_refusal, team_cases, verified

from threads.agents.store import open_store
from threads.log import ParseError, ThreadStartedEvent
from threads.result import Err
from threads.store import LOCAL_TENANT, VerifiedLog
from threads.team.cross import TeamLogEvents, check_team_logs
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


def _lead(logs: dict[str, VerifiedLog]) -> tuple[str, VerifiedLog, str]:
    for label, log in logs.items():
        started = next((e for e in log.fold.events if isinstance(e, ThreadStartedEvent)), None)
        if started is not None and started.data.team is not MISSING:
            return label, log, started.data.team.id
    raise AssertionError("no lead among the logs")


async def run(case: Path) -> Found:
    """The case's outcome: {states, index, tree}, or its first failure."""
    meta = _load(case / "case.json")
    inputs = meta["input"]
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
    store = await holding(raw, LOCAL_TENANT)
    sq = await open_store(store)
    label, lead, team = _lead(logs)
    rebuilt = await rebuild_team_index(sq, team)
    if isinstance(rebuilt, Err):
        return _failure(rebuilt.error, None)
    index = await sq.run(lambda c: index_rows(c, team))
    members = {(m.name, m.generation): m for m in await team_members(store, lead)}
    counted: list[JsonValue] = []
    pending: list[JsonValue] = []
    rows: list[tuple[str, int, str]] = await sq.run(
        lambda c: c.execute(
            "SELECT name, generation, role FROM team_members WHERE team_id = ?"
            " ORDER BY name, generation",
            (team,),
        ).fetchall()
    )
    for name, generation, role in rows:
        if role == "lead":  # where the walk starts: counted, never a child
            counted.append(name)
            continue
        opened = await open_member(store, lead, members[(name, generation)])
        if isinstance(opened, Err):
            return _failure(opened.error, label)
        (pending if opened.value == PENDING else counted).append(name)
    return {"states": states, "index": index, "tree": {"counted": counted, "pending": pending}}


def _cases() -> list[object]:
    corpus = [
        pytest.param(CASES / d.name, True, id=d.name)
        for d in sorted(CASES.iterdir())
        if json.loads((d / "case.json").read_text())["kind"] == "team"
    ]
    staged = [pytest.param(STAGED / name, False, id=f"staged/{name}") for name in team_cases()]
    return [*corpus, *staged]


@pytest.mark.parametrize(("case", "corpus"), _cases())
def test_team_case(case: Path, corpus: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    if not corpus:
        lift_refusal(monkeypatch)
    expected = _load(case / "expected.json")
    got = asyncio.run(run(case))
    if expected["outcome"] == "error":
        assert got == expected["error"]
        return
    assert "code" not in got, got
    if corpus:
        assert got["states"] == expected["states"]
    assert got["tree"] == expected["tree"]
    assert got["index"] == (
        _before_21a(expected["index"]) if case.name in NEEDS_21A else expected["index"]
    )


NEEDS_21A = frozenset({"team-failed-rebind-bounces"})
"""Until lane 21A makes a lead's message_received open a turn, the lead row stays as its
member_idle left it. Pinned exactly, so this fails (and goes) when 21A lands."""


def _before_21a(index: JsonValue) -> JsonValue:
    assert isinstance(index, dict)
    rows = index["team_members"]
    assert isinstance(rows, list)
    lead = {"state": "idle", "updated_seq": 29}
    fixed: list[JsonValue] = [
        {**r, **lead} if isinstance(r, dict) and r["role"] == "lead" else r for r in rows
    ]
    out: dict[str, JsonValue] = {**index, "team_members": fixed}
    return out
