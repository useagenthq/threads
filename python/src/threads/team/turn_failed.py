"""A host member's failed turn (spec/schema/README.md, "Teams Phase 2", semantic rule 53): a hop
cap, the caller's run budget, a model error or a turn-failing tool ends only that turn.

Its ending append is, in order: `turn_completed{reason, code?}`, one
`message_sent{kind: bounce, code: turn_failed, ask_id, error}` per ask the turn took and left
unanswered (all with one error, causal the turn_completed), then `member_idle{turn_failed}` with
that error. The row goes back to idle and keeps its last completed result. When the same append
ends the member instead, this rule does not apply: those asks close at their deadlines.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic import JsonValue

from threads.log import MailEnvelope, MessageReceivedEvent, TurnCompletedData
from threads.reduce import Fold
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.cross_host import reply_to
from threads.team.host_team import turn_failure_code
from threads.team.rows import own_rows, ref_of, team_row
from threads.team.settle import AppendContext


@dataclass(frozen=True, slots=True)
class TurnFailed:
    """What the failed turn recorded: the asks it bounced, in the order it took them."""

    failed: tuple[str, ...]


def fail_turn(
    ctx: AppendContext, fold: Fold, end: Mapping[str, JsonValue], error: Mapping[str, JsonValue]
) -> TurnFailed:
    """Appends the turn's ending events to the batch. `end` is the turn_completed's data and
    `error` the TurnFailure every bounce and the idle carry; a `budget_exceeded{scope: hop}`
    belongs before them and is the caller's to add."""
    _agrees(end, error)
    closed = ctx.batch.add(Draft("turn_completed", dict(end)))
    asks = tuple(fold.host.turn_asks)
    for ask_id in asks:
        ctx.batch.add(
            Draft("message_sent", {"envelope": _bounce(ctx, fold, ask_id, error, closed)})
        )
    ctx.batch.add(Draft("member_idle", {"turn_failed": dict(error)}))
    return TurnFailed(asks)


def _agrees(end: Mapping[str, JsonValue], error: Mapping[str, JsonValue]) -> None:
    code = turn_failure_code(TurnCompletedData.model_validate(dict(end)))
    if code is None or error.get("code") != code:
        raise AssertionError(f"a turn_failed error's code is not its turn end's: {code}")


def _bounce(
    ctx: AppendContext,
    fold: Fold,
    ask_id: str,
    error: Mapping[str, JsonValue],
    closed: str,
) -> dict[str, JsonValue]:
    """One ask's bounce: back to its sender, with the ask's provenance (rule 43) and the turn's
    error. Its causal is the turn_completed that failed."""
    ask = _taken(fold.events, ask_id)
    row = next(iter(own_rows(ctx.conn, ctx.thread_id)), None)
    team = None if row is None else team_row(ctx.conn, row.team_id)
    if row is None or team is None:
        raise AssertionError("a failed turn's member has a row")
    mail_id = ctx.batch.next_id()
    return {
        "mail_id": f"{ctx.branch_id}:{mail_id}",
        "kind": "bounce",
        "team": row.team_id,
        "from": ref_of(team, row),
        "to": reply_to(ask),
        "provenance": to_json(ask.provenance),
        "causal": {"thread_id": ctx.thread_id, "event_id": closed},
        "code": "turn_failed",
        "ask_id": ask_id,
        "error": dict(error),
    }


def _taken(events: Sequence[object], ask_id: str) -> MailEnvelope:
    found = next(
        (
            e.data.envelope
            for e in events
            if isinstance(e, MessageReceivedEvent) and e.data.envelope.ask_id == ask_id
        ),
        None,
    )
    if found is None:
        raise AssertionError(f"a turn_failed bounce names an ask this turn took: {ask_id}")
    return found
