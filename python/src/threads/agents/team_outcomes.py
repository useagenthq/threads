"""What the team log says of an operator's ask or wait (spec/api.json AskStatus, TeamAskResult,
TeamWaitResult): pure reads, hydrated as every result API is (a {ref} read back and verified,
StoreCorruptError when it can't be). Mirrors TypeScript's agent/team/outcomes.ts."""

import json
from collections.abc import Sequence

from pydantic import JsonValue, TypeAdapter
from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.member_results import hydrated, read_text
from threads.agents.store import now_ms
from threads.agents.team_answers import (
    Answered,
    AskCancelled,
    AskMemberEnded,
    AskOutcome,
    AskTimedOut,
    ParkedMember,
    Waited,
)
from threads.agents.team_handle_types import AskNotFound, AskOpen, AskStatus
from threads.log import (
    AskClosedEvent,
    BranchId,
    Event,
    MailEnvelope,
    MemberRef,
    MessageReceivedEvent,
    MessageSentEvent,
    StoredMemberResult,
    WaitFinishedEvent,
)
from threads.reduce.handlers import to_json
from threads.result import Err
from threads.store import SqliteStore
from threads.team.rows import team_row

_RESULT: TypeAdapter[StoredMemberResult] = TypeAdapter(StoredMemberResult)


async def _team_log(sq: SqliteStore, team: str) -> Sequence[Event]:
    row = await sq.run(lambda c: team_row(c, team))
    if row is None:
        raise AssertionError(f"no team {team}")
    read = await sq.read(BranchId(row.team_log_branch_id), now_ms())
    if isinstance(read, Err):
        raise AssertionError(f"team log of {team}: {read.error.message}")
    return read.value.fold.events


async def ask_status(sq: SqliteStore, team: str, ask_id: str) -> AskStatus:
    """An ask's state: open with its deadline, its outcome once closed, or not_found."""
    events = await _team_log(sq, team)
    closed = next(
        (e for e in events if isinstance(e, AskClosedEvent) and e.data.ask_id == ask_id), None
    )
    if closed is not None:
        return await _outcome(sq, events, closed)
    asked = next(
        (
            e.data.envelope
            for e in events
            if isinstance(e, MessageSentEvent)
            and e.data.envelope.kind == "ask"
            and e.data.envelope.ask_id == ask_id
        ),
        None,
    )
    if asked is None or not isinstance(asked.deadline, int):
        return AskNotFound(ask_id)
    return AskOpen(ask_id, asked.deadline)


async def ask_outcome(sq: SqliteStore, team: str, ask_id: str) -> AskOutcome | None:
    """The ask's outcome once the team log closed it."""
    got = await ask_status(sq, team, ask_id)
    return None if isinstance(got, AskOpen | AskNotFound) else got


async def _outcome(sq: SqliteStore, events: Sequence[Event], closed: AskClosedEvent) -> AskOutcome:
    ask_id = closed.data.ask_id
    outcome = to_json(closed.data.outcome)
    if not isinstance(outcome, dict):
        raise AssertionError("an ask_closed outcome is an object")
    match outcome.get("status"):
        case "answered":
            reply = next(
                (
                    e.data.envelope
                    for e in events
                    if isinstance(e, MessageReceivedEvent) and e.data.mail_id == outcome["reply"]
                ),
                None,
            )
            if reply is None or not isinstance(reply.from_, MemberRef):
                raise AssertionError(f"ask {ask_id} was answered by no received reply")
            return Answered(ask_id, await _text(sq, reply), reply.from_)
        case "member_ended":
            stored = _stored(outcome["result"])
            return AskMemberEnded(ask_id, await hydrated(stored, sq.get_artifact))
        case "timed_out":
            return AskTimedOut(ask_id)
        case _:
            return AskCancelled(ask_id)


def _stored(raw: JsonValue) -> StoredMemberResult:
    return _RESULT.validate_json(json.dumps(raw))


async def _text(sq: SqliteStore, env: MailEnvelope) -> str:
    body = env.body
    if body is MISSING:
        raise AssertionError("a reply has a text body")
    if body.text is not MISSING:
        return body.text
    if body.ref is MISSING:
        raise AssertionError("a text body is text or a ref")
    return await read_text(sq.get_artifact, body.ref)


async def wait_outcome(sq: SqliteStore, team: str, wait_id: str) -> Waited | None:
    """The wait's outcome once the team log finished it."""
    done = next(
        (
            e
            for e in await _team_log(sq, team)
            if isinstance(e, WaitFinishedEvent) and e.data.wait_id == wait_id
        ),
        None,
    )
    if done is None:
        return None
    d = done.data
    finished = tuple([await hydrated(r, sq.get_artifact) for r in d.finished])
    parked = tuple(ParkedMember(p.member, p.reason) for p in d.parked)
    return Waited("waited", finished, parked, tuple(d.pending), d.timed_out)
