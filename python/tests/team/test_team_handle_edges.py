"""The operator's handle at its edges: crash drills at each commit point of an operator request (and
of the team log's receipt), a dynamic member started by the operator, and hydration's
StoreCorruptError. Every test ends with the team's replay check. Mirrors TypeScript's
test/team/handle-edges.test.ts."""

import asyncio
import gc
import sqlite3
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Literal

import pytest
from pydantic import TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING
from team.run_kit import say, sq_of, start
from team.team_kit import assert_team_replays
from team.test_dynamic import scripted, specialist_of

from threads import (
    Completed,
    Principal,
    StoreCorruptError,
    Team,
    TeamAgent,
    TeamRef,
    agent,
    open_team,
    scripted_model,
    sqlite,
)
from threads.agents.member_results import MemberCompleted, hydrated
from threads.agents.store import now_ms, open_store
from threads.agents.team_handle_types import TeamStartRefused
from threads.agents.team_leads import leads_named
from threads.agents.team_log_mail import take_team_log_mail
from threads.agents.team_tools import Sent, Started
from threads.log import (
    ArtifactRef,
    BranchId,
    CompletedResult,
    Event,
    MailEnvelope,
    MemberRef,
    MemberStartedEvent,
    MessageSentEvent,
    ParseError,
    StoredMemberResult,
    TextBody,
)
from threads.log.digest import sha256_hex
from threads.result import Err, Ok
from threads.store.sql import blob_of
from threads.team.dynamic import InvalidDefinition
from threads.team.materialize import MaterializeOptions, Rebind, materialize
from threads.team.rows import team_row

OPERATOR = Principal(issuer="api", tenant="local", subject="operator")
BIG = "x" * 20_000
_RESULT: TypeAdapter[StoredMemberResult] = TypeAdapter(StoredMemberResult)


class CrashError(Exception):
    """The process dying at a commit point."""


class _Crashing(sqlite3.Connection):
    """A connection that dies once, at the first statement holding `point`."""

    point: str | None = None

    def execute(self, sql: str, parameters: Sequence[object] = (), /) -> sqlite3.Cursor:  # type: ignore[override] - narrowed for the drill
        point = type(self).point
        if point is not None and point in sql:
            type(self).point = None
            raise CrashError(sql)
        return super().execute(sql, parameters)


async def _world(
    path: Path, m: pytest.MonkeyPatch, *, lead_starts: bool = False
) -> tuple[TeamRef, TeamAgent[None, str]]:
    """A lead that ran once in a store whose connection can die, its team, and the lead itself:
    open_team rebinds it while the caller holds it."""
    store = sqlite(str(path))
    m.setattr(sqlite3, "connect", partial(sqlite3.connect, factory=_Crashing))
    await open_store(store)
    member = agent(
        name="writer", model=scripted_model({"responses": [say(BIG if lead_starts else "Ok.")]})
    )
    opening = [start("c1", "writer", "Go.")] if lead_starts else []
    script = [*opening, say("Ready."), say("Done.")]
    lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
    r = await lead.run("Get ready.", store=store)
    assert isinstance(r, Completed)
    return r.team.ref, lead


async def _fresh(path: Path, ref: TeamRef) -> Team:
    """The team through a new store on the same database: the restart after a crash."""
    opened = await open_team(sqlite(str(path)), ref, principal=OPERATOR)
    assert isinstance(opened, Ok)
    return opened.value


async def _team_log(path: Path, ref: TeamRef) -> Sequence[Event]:
    sq = await open_store(sqlite(str(path)))
    row = await sq.run(lambda c: team_row(c, ref.id))
    assert row is not None
    read = await sq.read(BranchId(row.team_log_branch_id), now_ms())
    assert isinstance(read, Ok)
    return read.value.fold.events


def test_a_crash_inside_team_start_stores_none_of_it_the_keyed_retry_starts_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        with monkeypatch.context() as m:
            ref, _lead = await _world(tmp_path, m)
            dying = await _fresh(tmp_path, ref)
            _Crashing.point = "INSERT INTO operator_receipts"
            with pytest.raises(CrashError):
                await dying.start("writer", "Draft.", idempotency_key="k")
        assert _Crashing.point is None, "the drill reached its commit point"
        team = await _fresh(tmp_path, ref)
        assert not [e for e in await _team_log(tmp_path, ref) if e.type == "operator_request"]
        retried = await team.start("writer", "Draft.", idempotency_key="k")
        assert isinstance(retried, Started)
        # A second retry (the answer was lost after the commit) replays it.
        assert await team.start("writer", "Draft.", idempotency_key="k") == retried
        log = await _team_log(tmp_path, ref)
        assert len([e for e in log if isinstance(e, MemberStartedEvent)]) == 1
        await assert_team_replays(await open_store(sqlite(str(tmp_path))), ref.id)

    asyncio.run(main())


def test_a_crash_inside_team_send_stores_none_of_it_the_keyed_retry_sends_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        with monkeypatch.context() as m:
            ref, _lead = await _world(tmp_path, m)
            dying = await _fresh(tmp_path, ref)
            started = await dying.start("writer", "Draft.")
            assert isinstance(started, Started)
            _Crashing.point = "INSERT INTO mail"
            with pytest.raises(CrashError):
                await dying.send(started.member, "Short.", idempotency_key="s")
        team = await _fresh(tmp_path, ref)
        sent = await team.send(started.member, "Short.", idempotency_key="s")
        assert isinstance(sent, Sent)
        messages = [
            e
            for e in await _team_log(tmp_path, ref)
            if isinstance(e, MessageSentEvent) and e.data.envelope.kind == "message"
        ]
        assert len(messages) == 1
        await assert_team_replays(await open_store(sqlite(str(tmp_path))), ref.id)

    asyncio.run(main())


def test_a_crash_inside_the_team_logs_receipt_leaves_the_notice_pending_the_next_step_takes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        with monkeypatch.context() as m:
            ref, _lead = await _world(tmp_path, m)
            team = await _fresh(tmp_path, ref)
            await team.start("writer", "Draft.")
            sq = await open_store(sqlite(str(tmp_path)))

            async def unavailable(_s: MemberStartedEvent, _t: MailEnvelope) -> Rebind:
                return Rebind("pin_unavailable")

            # The rebind fails: the member ends at once and notifies its starter, the team log.
            o = MaterializeOptions(unavailable, "test", 30_000, now_ms)
            assert isinstance(await materialize(sq, ref.id, "writer-1", o), Ok)
            _Crashing.point = "UPDATE mail SET state = 'consumed'"
            with pytest.raises(CrashError):
                await take_team_log_mail(sq, ref.id, None)
        sq = await open_store(sqlite(str(tmp_path)))
        assert not [e for e in await _team_log(tmp_path, ref) if e.type == "message_received"]
        await take_team_log_mail(sq, ref.id, None)
        await take_team_log_mail(sq, ref.id, None)
        received = [e for e in await _team_log(tmp_path, ref) if e.type == "message_received"]
        assert len(received) == 1
        await assert_team_replays(sq, ref.id)

    asyncio.run(main())


def test_members_reads_a_large_output_from_the_artifact_store() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        member = agent(name="writer", model=scripted_model({"responses": [say(BIG)]}))
        script = [start("c1", "writer", "Go."), say("Ready."), say("Done.")]
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        r = await lead.run("Get ready.", store=store)
        assert isinstance(r, Completed)
        writer = next(m for m in await r.team.members() if m.name == "writer-1")
        assert isinstance(writer.result, MemberCompleted)
        assert writer.result.output == BIG
        await assert_team_replays(await sq_of(store), r.team.ref.id)

    asyncio.run(main())


@pytest.mark.parametrize("how", ["missing", "short"])
def test_a_broken_output_artifact_raises_store_corrupt_error(
    how: Literal["missing", "short"], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        member = agent(name="writer", model=scripted_model({"responses": [say(BIG)]}))
        script = [start("c1", "writer", "Go."), say("Ready."), say("Done.")]
        lead = agent(name="lead", model=scripted_model({"responses": script}), team=[member])
        r = await lead.run("Get ready.", store=store)
        assert isinstance(r, Completed)
        sq = await sq_of(store)
        raw = await sq.run(
            lambda c: c.execute(
                "SELECT result FROM team_members WHERE name = 'writer-1'"
            ).fetchall()
        )
        result = _RESULT.validate_json(blob_of(raw[0][0]))
        assert isinstance(result, CompletedResult)
        ref = result.output.ref
        assert ref is not MISSING
        real = sq.get_artifact

        async def broken(sha: str) -> Ok[bytes] | Err[ParseError]:
            got = await real(sha)
            if sha != ref.sha256 or isinstance(got, Err):
                return got
            if how == "missing":
                return Err(ParseError("artifact_missing", f"no artifact {sha}"))
            return Ok(got.value[1:])

        monkeypatch.setattr(sq, "get_artifact", broken)
        with pytest.raises(StoreCorruptError) as raised:
            await r.team.members()
        code = "artifact_missing" if how == "missing" else "artifact_corrupt"
        assert raised.value.code == code
        assert raised.value.ref == ref
        # The audit feed returns results as stored, and never raises.
        assert [item async for item in r.team.events()]
        await assert_team_replays(sq, r.team.ref.id)

    asyncio.run(main())


def test_team_start_with_a_dynamic_agents_fields_and_its_refusals_detail_replayed() -> None:
    async def main() -> None:
        store = sqlite(":memory:")
        lead = agent(
            name="lead",
            model=scripted_model({"responses": [say("Ready.")]}),
            team=[specialist_of(scripted([say("Paid.")]), scripted())],
        )
        r = await lead.run("Get ready.", store=store)
        assert isinstance(r, Completed)
        team = r.team
        started = await team.start(
            "specialist",
            "Is INV-1002 paid?",
            label="checker",
            instructions="Answer yes or no.",
            tools=["invoice_status"],
            model="strong",
        )
        assert isinstance(started, Started)
        sq = await sq_of(store)
        row = await sq.run(lambda c: team_row(c, team.ref.id))
        assert row is not None
        read = await sq.read(BranchId(row.team_log_branch_id), now_ms())
        assert isinstance(read, Ok)
        member = next(e for e in read.value.fold.events if isinstance(e, MemberStartedEvent))
        define = member.data.define
        assert define is not MISSING
        assert list(define.tools) == ["invoice_status"]
        assert define.model == "strong"
        assert member.data.label == "checker"
        listed = await team.members()
        assert next(m for m in listed if m.name == "specialist-1").label == "checker"
        refused = await team.start("specialist", "Push.", tools=["git_push"], idempotency_key="b")
        allowed = ("invoice_status", "read_notes")
        detail = InvalidDefinition("tools", "not_allowed", allowed)
        assert refused == TeamStartRefused("invalid_definition", detail=detail)
        again = await team.start("specialist", "Push.", tools=["git_push"], idempotency_key="b")
        assert again == refused
        await assert_team_replays(sq, team.ref.id)

    asyncio.run(main())


def test_an_output_artifact_that_is_not_utf8_raises_store_corrupt_error() -> None:
    async def main() -> None:
        data = b"\xff\xfe"
        ref = ArtifactRef(sha256=sha256_hex(data), bytes=len(data), media_type="text/plain")
        team = "0192c000-0000-7000-8000-000000000001"
        member = MemberRef(tenant="local", team=team, name="writer-1", generation=1)
        stored = CompletedResult(member=member, output=TextBody(ref=ref), status="completed")

        async def read(_sha: str) -> Ok[bytes] | Err[ParseError]:
            return Ok(data)

        with pytest.raises(StoreCorruptError) as raised:
            await hydrated(stored, read)
        assert raised.value.code == "artifact_corrupt"

    asyncio.run(main())


def test_a_lead_nobody_holds_is_dropped_from_the_registry() -> None:
    name = "a-lead-to-drop"
    lead = agent(name=name, model=scripted_model({"responses": []}), team=[])
    assert len(leads_named(name)) == 1
    del lead
    gc.collect()
    assert leads_named(name) == ()
