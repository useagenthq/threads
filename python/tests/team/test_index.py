"""The team index (spec/schema/README.md, "Teams", the replay rule): the rows the writer's
insert_rows/change_rows produce are exactly the ones a rebuild from the logs alone produces, and
both equal the reference fold pinned in every staged team case's `index`."""

import asyncio
import json
import sqlite3

import pytest
from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING
from team.team_kit import (
    STAGED,
    TEAM,
    index_rows,
    lift_refusal,
    rechain,
    staged,
    stored,
    team_cases,
    verified,
)

from threads.log import (
    Event,
    MailRefusedEvent,
    MessageReceivedEvent,
    MessageSentEvent,
    ThreadStartedEvent,
    UserInputEvent,
)
from threads.result import Err, Ok
from threads.team.index import TeamLog, change_rows, insert_rows, turn_openers
from threads.team.rebuild import rebuild_team_index

WITH_INDEX = [
    c for c in team_cases() if "index" in json.loads((STAGED / c / "expected.json").read_text())
]
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
NEEDS_21A = "lane 21A: the lead's message_received opens a turn (the reducer doesn't yet)"


def _expected(case: str) -> JsonValue:
    expected = _JSON.validate_json((STAGED / case / "expected.json").read_bytes())
    assert isinstance(expected, dict)
    return expected["index"]


def _cases() -> list[object]:
    return [
        pytest.param(c, marks=pytest.mark.xfail(strict=True, reason=NEEDS_21A))
        if c == "team-failed-rebind-bounces"
        else c
        for c in WITH_INDEX
    ]


@pytest.mark.parametrize("case", _cases())
def test_rebuild_equals_the_reference_fold(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main() -> dict[str, JsonValue]:
        store = await stored(staged(case))
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        return await store.run(lambda c: index_rows(c, TEAM))

    assert asyncio.run(main()) == _expected(case)


@pytest.mark.parametrize("case", WITH_INDEX)
def test_append_time_writes_equal_the_rebuild(case: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The writer's path: one event at a time, in an order where every sender appended before
    its recipient. The rebuild after a wipe gives the same rows, the feed aside."""
    lift_refusal(monkeypatch)

    async def main() -> tuple[JsonValue, JsonValue]:
        store = await stored(staged(case))
        queues: list[tuple[TeamLog, list[Event], frozenset[str]]] = []
        for label, raw in staged(case).items():
            log = verified(raw)
            assert isinstance(log, Ok), label
            fold = log.value.fold
            branch = log.value.segments[-1].header.branch_id
            assert fold.thread_id is not None
            team_log = TeamLog(fold.thread_id, branch)
            queues.append((team_log, list(fold.events), turn_openers(log.value)))
        await store.run(lambda c: _append_all(c, queues))
        written = await store.run(lambda c: index_rows(c, TEAM))
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        rebuilt = await store.run(lambda c: index_rows(c, TEAM))
        return _without_feed(written), _without_feed(rebuilt)

    written, rebuilt = asyncio.run(main())
    assert written == rebuilt


def _append_all(
    conn: sqlite3.Connection, queues: list[tuple[TeamLog, list[Event], frozenset[str]]]
) -> None:
    """Appends every event, each as its own append (insert, then change), always taking the
    first log whose next event finds the row it changes; a receipt whose sender is not among the
    logs goes last."""
    while any(events for _, events, _ in queues):
        ready = [q for q in queues if q[1] and _ready(conn, q[1][0])]
        log, events, opened = ready[0] if ready else next(q for q in queues if q[1])
        event = events.pop(0)
        insert_rows(conn, log, [event])
        change_rows(conn, log, [event], opened)


def _ready(conn: sqlite3.Connection, e: Event) -> bool:
    """Whether the row this event changes, if any, exists yet."""
    if isinstance(e, MessageReceivedEvent | UserInputEvent | MailRefusedEvent):
        mail = e.data.mail_id
        return mail is MISSING or _exists(conn, "SELECT 1 FROM mail WHERE mail_id = ?", mail)
    if isinstance(e, ThreadStartedEvent) and e.data.parent is not MISSING:
        return _exists(conn, "SELECT 1 FROM team_members WHERE thread_id = ?", e.thread_id)
    if isinstance(e, MessageSentEvent) and e.data.envelope.monitor_id is not MISSING:
        monitor = e.data.envelope.monitor_id
        return _exists(conn, "SELECT 1 FROM monitors WHERE monitor_id = ?", monitor)
    return True


def _exists(conn: sqlite3.Connection, query: str, key: object) -> bool:
    return conn.execute(query, (key,)).fetchone() is not None


def test_rebuild_of_an_unknown_team_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main() -> None:
        store = await stored(staged("team-settle-wakes-lead"))
        found = await rebuild_team_index(store, "0192c000-0000-7000-8000-00000000ffff")
        assert isinstance(found, Err)
        assert found.error.code == "not_found"

    asyncio.run(main())


def test_a_second_rebuild_starts_a_new_feed_epoch(monkeypatch: pytest.MonkeyPatch) -> None:
    lift_refusal(monkeypatch)

    async def main() -> None:
        store = await stored(staged("team-settle-wakes-lead"))
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        assert await rebuild_team_index(store, TEAM) == Ok(None)
        epochs = await store.run(
            lambda c: c.execute(
                "SELECT DISTINCT epoch FROM team_feed WHERE team_id = ?", (TEAM,)
            ).fetchall()
        )
        assert epochs == [(2,)]

    asyncio.run(main())


def test_a_rebuild_refuses_a_forged_receipt_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 43 guards every rebuild: a receipt whose body differs from its sender's."""
    lift_refusal(monkeypatch)
    logs = staged("team-settle-wakes-lead")

    def forge(events: list[dict[str, JsonValue]]) -> list[dict[str, JsonValue]]:
        for e in events:
            if e["type"] == "message_received":
                data = e["data"]
                assert isinstance(data, dict)
                env = data["envelope"]
                assert isinstance(env, dict)
                env["causal"] = {
                    **_dict(env["causal"]),
                    "event_id": "0192e001-0000-7000-8000-0000000000ff",
                }
        return events

    logs["lead"] = rechain(logs["lead"], forge)

    async def main() -> None:
        store = await stored(logs)
        found = await rebuild_team_index(store, TEAM)
        assert isinstance(found, Err)
        assert found.error.code == "invalid_transition"
        count = await store.run(lambda c: c.execute("SELECT COUNT(*) FROM team_members").fetchone())
        assert count == (0,)

    asyncio.run(main())


def _dict(value: JsonValue) -> dict[str, JsonValue]:
    assert isinstance(value, dict)
    return value


def _without_feed(rows: JsonValue) -> JsonValue:
    assert isinstance(rows, dict)
    return {k: v for k, v in rows.items() if k != "team_feed"}
