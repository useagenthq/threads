"""What one team append sees of its own log while it is being built: the committed events plus the
batch's drafts. The index moves only at commit, so a read later in the same batch must skip what
the batch already took, closed, finished or resumed (the reference re-reads its log after every
event it adds: spec/tools/fixtures/ops_world.py)."""

from collections.abc import Sequence

from pydantic import JsonValue

from threads.log import MailEnvelope, ParkAddress, WaitStartedData
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.store.conn import Conn
from threads.team.batch import Batch
from threads.team.rows import ask_row

type Item = tuple[str, str, dict[str, JsonValue]]
"""(type, event_id, data): an event of this log, committed or in the batch."""


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise AssertionError("an event's data is an object")
    return value


def items_of(fold: Fold, batch: Batch, types: frozenset[str]) -> list[Item]:
    """The committed events, then the batch's drafts (each with its minted event_id), of
    `types`."""
    out = [
        (e.type, str(e.event_id), _object(to_json(e.data))) for e in fold.events if e.type in types
    ]
    out += [
        (d.type, d.event_id, dict(d.data))
        for d in batch.drafts
        if d.type in types and d.event_id is not None
    ]
    return out


def parks_now(fold: Fold, batch: Batch) -> list[ParkAddress]:
    """The parks open once the batch commits: the fold's, less those it resumes, plus its own."""
    parks = list(fold.parked)
    for d in batch.drafts:
        if d.type in ("resumed", "parked"):
            address = ParkAddress.model_validate(d.data["address"])
            parks = [p for p in parks if p != address] if d.type == "resumed" else [*parks, address]
    return parks


def parked_on(fold: Fold, batch: Batch, address: ParkAddress) -> bool:
    return address in parks_now(fold, batch)


def open_waits(fold: Fold, batch: Batch) -> set[str]:
    """This log's waits still open once the batch commits."""
    open_ = set(fold.team.waits)
    for d in batch.drafts:
        wait = d.data.get("wait_id")
        if d.type == "wait_started" and isinstance(wait, str):
            open_.add(wait)
        elif d.type == "wait_finished" and isinstance(wait, str):
            open_.discard(wait)
    return open_


def settle_monitors(fold: Fold, batch: Batch, branch: str) -> dict[str, str]:
    """Every settle MonitorId this log registered (the batch's too), mapped to its WaitId."""
    out: dict[str, str] = {}
    for _, event_id, data in items_of(fold, batch, frozenset({"wait_started"})):
        started = WaitStartedData.model_validate(data)
        for m in started.members:
            out[f"{branch}:{event_id}:{m.name}"] = started.wait_id
    return out


def ask_open(conn: Conn, branch: str, ask_id: str, batch: Batch) -> bool:
    """The ask is this branch's and open: its row says so and the batch hasn't closed it."""
    closed = any(d.type == "ask_closed" and d.data.get("ask_id") == ask_id for d in batch.drafts)
    row = ask_row(conn, ask_id)
    return not closed and row is not None and row.state == "open" and row.asker_branch_id == branch


def untaken(pending: Sequence[MailEnvelope], batch: Batch) -> list[MailEnvelope]:
    """Pending mail the batch hasn't taken yet."""
    taken = batch.taken()
    return [m for m in pending if m.mail_id not in taken]
