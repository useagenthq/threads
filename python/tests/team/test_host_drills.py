"""The Teams Phase 2 crash drills (lane 29D) around a host member's mail: a kill at each commit
point, then a restart that finishes the work exactly once. The lazy open's own drills are in
test_host_open_drills.py.

1. A caller's ask is committed and the host is killed inside billing's consume: the rebuild
   finds it through the caller's log alone (D.4).
2. Billing's reply is committed and the host is killed inside the caller's consume: the
   caller's park resumes once, with one `tool_result`.
3. A hop cap is exhausted mid-turn and the process is killed after the turn's ending append:
   billing is back to `idle`, the ask is `failed`, and there is no restart and no member_ended.
4. A host member's end bounces the ask its turn had taken (decision 6).

Every one of them ends in the replay check. The same drills as TypeScript's
test/team/host-drills.test.ts.
"""

import asyncio
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import JsonValue
from team.crash_kit import NEVER, CrashError, Point, arm, crashing, reached
from team.team_kit import (
    CASES,
    Line,
    add,
    assert_team_replays,
    case_logs,
    rechain,
    stored,
)

from threadsai import sqlite
from threadsai.agents.store import now_ms, open_store, scoped
from threadsai.agents.team_units import take_mail
from threadsai.log import BranchId, MemberEndedEvent, MemberStartedEvent
from threadsai.result import Ok
from threadsai.store import Draft, SqliteStore
from threadsai.store.conn import Conn
from threadsai.store.writer import DecideTx, Refusal
from threadsai.team.batch import Batch
from threadsai.team.host_team import host_team_ids
from threadsai.team.provenance import turn_provenance
from threadsai.team.rebuild import rebuild_team_index
from threadsai.team.rows import ask_row, member_rows
from threadsai.team.settle import SettleContext, settle

TENANT = "acme"
IDS = host_team_ids(TENANT)
CALLER = BranchId("0192b000-0000-7000-8000-0000000000d1")
BILLING = BranchId("0192b000-0000-7000-8000-0000000000c1")
CONFIG = "5800e46921bd898ffefe26cbb45e8038fd946b9719bd1f0e155c1d94aa9f459b"
MEMBERS = {"billing": CONFIG}


# ---------- 2: the caller's log is the only record of a pending ask ----------


CONSUMED = Point("a mail row's consume", lambda _c, sql, _p: "UPDATE mail SET state" in sql)


def _expire_leases(conn: Conn) -> None:
    conn.execute("UPDATE leases SET expires_at = 0")


async def _seeded(where: Path, monkeypatch: pytest.MonkeyPatch, case: str) -> SqliteStore:
    """The case's logs on a crashing file store, its index built, no point armed yet."""
    store = await crashing(where, monkeypatch, NEVER)
    sq = await open_store(scoped(store, TENANT))
    await add(sq, case_logs(case), TENANT)
    assert await rebuild_team_index(sq, IDS.team) == Ok(None)
    return sq


def test_a_kill_before_billings_consume_leaves_the_ask_only_in_the_callers_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The caller's ask is committed; the host dies inside billing's consume, so nothing of it
    lands. Only the caller's log records the ask, and the rebuild finds it there (D.4)."""

    async def main() -> None:
        sq = await _seeded(
            tmp_path / "t.db", monkeypatch, "host-caller-ask-pending-only-in-caller-log"
        )
        arm(CONSUMED)
        with pytest.raises(CrashError):
            await take_mail(sq, None, BILLING)
        assert reached(), "the drill reached its commit point"
        again = await open_store(scoped(sqlite(str(tmp_path / "t.db")), TENANT))
        assert await _types(again, BILLING) == ["thread_started"]
        # A fresh process wipes and refolds: the ask comes back through the caller's log.
        assert await rebuild_team_index(again, IDS.team) == Ok(None)
        row = await again.run(lambda c: ask_row(c, f"{CALLER}:c1"))
        assert row is not None
        assert (row.state, row.asker_branch_id) == ("open", CALLER)
        await assert_team_replays(again, IDS.team)

    asyncio.run(main())


def test_a_rebuild_finds_a_pending_ask_through_the_callers_log_alone() -> None:
    case = "host-caller-ask-pending-only-in-caller-log"

    async def main() -> None:
        logs = case_logs(case)
        assert set(logs) == {"team", "billing", "support"}
        store = await stored(logs)
        assert await rebuild_team_index(store, IDS.team) == Ok(None)
        row = await store.run(lambda c: ask_row(c, f"{CALLER}:c1"))
        assert row is not None
        assert (row.state, row.asker_branch_id) == ("open", CALLER)
        # Without the caller's log nothing names the ask: billing never consumed it, so the
        # caller scan is the only way the rebuild can see it.
        without = await stored({k: v for k, v in logs.items() if k != "support"})
        assert await rebuild_team_index(without, IDS.team) == Ok(None)
        assert await without.run(lambda c: ask_row(c, f"{CALLER}:c1")) is None
        await assert_team_replays(store, IDS.team)

    asyncio.run(main())


# ---------- 3: the caller's park resumes once ----------


async def _types(sq: SqliteStore, branch: BranchId) -> list[str]:
    read = await sq.read(branch, now_ms())
    assert isinstance(read, Ok), read
    return [e.type for e in read.value.fold.events]


def test_a_kill_inside_the_callers_consume_leaves_it_to_resume_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """billing's reply is committed; the host dies inside the caller's consume. Nothing of that
    append lands, and after the restart the caller takes it once."""

    async def main() -> None:
        sq = await _seeded(tmp_path / "t.db", monkeypatch, "host-caller-reply-unconsumed")
        before = await _types(sq, CALLER)
        arm(CONSUMED)
        with pytest.raises(CrashError):
            await take_mail(sq, None, CALLER)
        assert reached(), "the drill reached its commit point"
        again = await open_store(scoped(sqlite(str(tmp_path / "t.db")), TENANT))
        assert await _types(again, CALLER) == before
        # The dead process's lease outlives it until its TTL; a restart waits that out.
        await again.run(_expire_leases)
        taken = await take_mail(again, None, CALLER)
        assert taken is not None
        assert taken.status == "consumed"
        added = (await _types(again, CALLER))[len(before) :]
        assert added == ["message_received", "ask_closed", "resumed", "tool_result"]
        await assert_team_replays(again, IDS.team)

    asyncio.run(main())


def test_an_unconsumed_reply_resumes_the_callers_park_exactly_once() -> None:
    async def main() -> None:
        store = await stored(case_logs("host-caller-reply-unconsumed"))
        assert await rebuild_team_index(store, IDS.team) == Ok(None)
        before = await _types(store, CALLER)
        assert "ask_closed" not in before
        taken = await take_mail(store, None, CALLER)
        assert taken is not None
        assert taken.status == "consumed"
        after = await _types(store, CALLER)
        added = after[len(before) :]
        assert added == ["message_received", "ask_closed", "resumed", "tool_result"]
        # The row moved, so a second pass takes nothing and records no second result.
        again = await take_mail(store, None, CALLER)
        assert again is not None
        assert again.status == "nothing_pending"
        assert await _types(store, CALLER) == after
        row = await store.run(lambda c: ask_row(c, f"{CALLER}:c1"))
        assert row is not None
        assert row.state == "answered"
        await assert_team_replays(store, IDS.team)

    asyncio.run(main())


# ---------- 4: a hop cap ends only the turn ----------


def test_a_hop_cap_leaves_the_member_idle_the_ask_failed_and_no_restart() -> None:
    async def main() -> None:
        store = await stored(case_logs("host-member-hop-capped"))
        assert await rebuild_team_index(store, IDS.team) == Ok(None)
        rows = await store.run(lambda c: member_rows(c, IDS.team))
        assert [(r.name, r.generation, r.state) for r in rows] == [("billing", 1, "idle")]
        ask = await store.run(lambda c: ask_row(c, f"{CALLER}:c1"))
        assert ask is not None
        assert ask.state == "failed"
        own = await _types(store, BILLING)
        assert "member_idle" in own
        assert "member_ended" not in own
        read = await store.read(IDS.log_branch_id, now_ms())
        assert isinstance(read, Ok), read
        log = read.value.fold.events
        assert not [e for e in log if isinstance(e, MemberEndedEvent)]
        # One generation: a failed turn is no reason to supervise or restart.
        assert [e.data.member.generation for e in log if isinstance(e, MemberStartedEvent)] == [1]
        await assert_team_replays(store, IDS.team)

    asyncio.run(main())


# ---------- 5: a host member's end bounces the ask its turn took ----------

_ENDED = "host-member-end-bounces-taken-ask"
_BUDGET: dict[str, JsonValue] = {
    "limit": "max_cost_nanos",
    "limit_value": 5_000_000_000,
    "observed": 5_200_000_000,
    "observed_is_upper_bound": False,
    "scope": "thread",
}


def _first_two(events: list[Line]) -> list[Line]:
    """billing's log up to the ask it consumed: its own budget has not run out yet."""
    return events[:2]


def test_a_host_members_end_bounces_the_ask_its_turn_took_instead_of_timing_it_out() -> None:
    """Coordinator decision 6: the ask was consumed, so the end's refusal of pending mail can
    never reach it. Its asker closes it member_ended at once, not at its deadline."""

    async def main() -> None:
        logs = case_logs(_ENDED)
        logs["billing"] = rechain(logs["billing"], _first_two)
        store = await stored(logs)
        assert await rebuild_team_index(store, IDS.team) == Ok(None)
        before = await _types(store, BILLING)
        assert before == ["thread_started", "message_received"]
        await _end_over_budget(store)
        assert await _types(store, BILLING) == [
            *before,
            "budget_exceeded",
            "turn_completed",
            "member_ended",
            "message_sent",
        ]
        taken = await take_mail(store, None, CALLER)
        assert taken is not None
        assert taken.status == "consumed"
        row = await store.run(lambda c: ask_row(c, f"{CALLER}:c1"))
        assert row is not None
        assert row.state == "member_ended"
        await assert_team_replays(store, IDS.team)

    asyncio.run(main())


async def _end_over_budget(store: SqliteStore) -> None:
    """billing's own thread budget runs out in the turn that took the ask."""
    got = await store.acquire(BILLING, "drill", now_ms)
    assert isinstance(got, Ok), got
    writer = got.value

    def decide(tx: DecideTx) -> Sequence[Draft] | Refusal[None]:
        batch = Batch(tx.fold.seq, tx.now, None)
        batch.add(Draft("budget_exceeded", dict(_BUDGET)))
        batch.add(Draft("turn_completed", {"reason": "budget_exhausted"}))
        provenance = turn_provenance(tx.conn, tx.fold.events)
        ctx = SettleContext(
            tx.conn,
            batch,
            str(tx.fold.thread_id),
            writer.branch_id,
            provenance,
            _no_text,
            tuple(tx.fold.host.turn_asks),
        )
        settle(ctx, {"status": "budget_exhausted", "budget": dict(_BUDGET)})
        return batch.drafts

    done = await writer.append_decided(decide)
    await writer.release()
    assert isinstance(done, Ok), done


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("this drill's result has no text")


def test_the_drill_cases_are_in_the_corpus() -> None:
    """A case that moves out from under these drills fails here, not silently."""
    for case in (
        "host-caller-ask-pending-only-in-caller-log",
        "host-caller-reply-unconsumed",
        "host-member-hop-capped",
        _ENDED,
    ):
        assert (CASES / case / "case.json").exists(), case
