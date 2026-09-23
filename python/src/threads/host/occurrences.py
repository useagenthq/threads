"""Deciding a schedule thread's pending occurrences.

Only the log proves an overlap (an open turn); a busy writer proves nothing, since outbound,
intake or a control may hold it briefly. Every decision is appended under the thread's writer, in
occurrence order, with the row's conditional update in the same transaction.
"""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from pydantic import JsonValue, TypeAdapter

from threads.agents.store import Store, now_ms, open_store
from threads.host.runs import Runner
from threads.log import BranchId, Principal, ThreadId
from threads.reduce.handlers import to_json
from threads.result import Ok
from threads.store import Draft, SqliteStore, Writer
from threads.store.lines import uuid7
from threads.store.schedules import Pending, Reason, decided
from threads.thread.handle import Thread

HOLD_TRIES: Final = 10
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


@dataclass(frozen=True, slots=True)
class Pass:
    """One scheduler pass over a tenant."""

    runner: Runner
    store: Store
    """The tenant's view of the host store."""
    tenant: str


async def decide_thread(p: Pass, thread_id: ThreadId) -> None:
    """Decides the thread's pending rows and starts the run of the one it fires, if any."""
    sq = await open_store(p.store)
    root = await sq.root(thread_id)
    read = await sq.read(root.value, now_ms()) if isinstance(root, Ok) else None
    if not isinstance(root, Ok) or not isinstance(read, Ok):
        return
    rows = sq.tables.schedules
    if read.value.fold.in_turn:
        await rows.mark_overlaps(thread_id)
    writer = await _briefly(sq, root.value)
    if writer is None:
        # Contention is not an overlap: the rows stay pending for the next tick.
        return
    fired: Pending | None = None
    try:
        for row in await rows.pending(thread_id):
            reason = _classify(row, in_turn=writer.fold.in_turn, runner=p.runner)
            if not await log_occurrence(writer, p.tenant, row, reason):
                # Another scheduler decided it first: its state is current, so stop here.
                break
            if reason is None:
                fired = row
    finally:
        await writer.release()
    if fired is not None and p.runner.agent(fired.agent) is not None:
        thread = Thread(thread_id, root.value, p.store)
        who = principal(p.tenant, fired.schedule_id)
        p.runner.launch(p.runner.bound_to(fired.agent), None, thread, who)


def _classify(row: Pending, *, in_turn: bool, runner: Runner) -> Reason | None:
    """In order: missed, then an open turn, then fire with the frozen agent if it is served."""
    if row.reason is not None:
        return row.reason
    if in_turn:
        return "overlap"
    return None if runner.agent(row.agent) is not None else "removed"


async def log_occurrence(writer: Writer, tenant: str, row: Pending, reason: Reason | None) -> bool:
    """Appends the row's event and decides the row in one transaction. False when the row is no
    longer pending (a stale copy): the append rolls back and nothing is logged."""
    actor: dict[str, JsonValue] = {
        "kind": "scheduler",
        "principal": to_json(principal(tenant, row.schedule_id)),
    }
    data: dict[str, JsonValue] = {
        "schedule_id": row.schedule_id,
        "occurrence_id": f"{row.schedule_id}@{_iso(row.occurrence_at)}",
        "scheduled_for": row.occurrence_at,
        "timezone": row.timezone,
    }
    if reason is not None:
        drafts = [Draft("schedule_skipped", {**data, "reason": reason}, actor, critical=False)]
    else:
        fired = uuid7(now_ms())
        drafts = [
            Draft("schedule_fired", data, actor, True, fired),
            Draft("user_input", _input(row, fired), actor),
        ]
    done = await writer.append(drafts, decided(tenant, row, reason))
    return isinstance(done, Ok)


def _input(row: Pending, cause: str) -> dict[str, JsonValue]:
    # The frozen input the reservation wrote; the line's own schema checks it on append.
    given = _JSON.validate_json(row.input_json)
    body: dict[str, JsonValue] = {"text": given} if isinstance(given, str) else {"content": given}
    return {"source": "schedule", "delivery_event_id": cause, **body}


def principal(tenant: str, schedule_id: str) -> Principal:
    """A scheduled run's caller: the schedule itself (spec/api.json Schedule.id)."""
    return Principal(issuer="schedule", tenant=tenant, subject=schedule_id)


def _iso(ms: int) -> str:
    """RFC 3339 UTC with milliseconds, as JS Date.toISOString writes it."""
    return f"{datetime.fromtimestamp(ms // 1000, UTC):%Y-%m-%dT%H:%M:%S}.{ms % 1000:03d}Z"


async def _briefly(sq: SqliteStore, branch: BranchId) -> Writer | None:
    """The writer, waiting out another scheduler's short hold; a run's longer hold stays busy."""
    for tried in range(HOLD_TRIES + 1):
        taken = await sq.acquire(branch, f"schedule-{uuid.uuid4().hex}", now_ms)
        if isinstance(taken, Ok):
            return taken.value
        if tried < HOLD_TRIES:
            await asyncio.sleep(0.02)
    return None
