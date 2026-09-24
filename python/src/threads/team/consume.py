"""mail.consume under the recipient's writer (spec/schema/README.md, "Teams"; design §4.7): the
pending rows in (created_at, mail_id) order, in one pass. Control mail is taken as it comes,
whatever its provenance; ordinary mail only when the recipient was not parked when the consume
began, as one batch of one (principal, root_request): mid-turn the open turn's, else the first
row's, ending at the first row of another. A member run takes only ordinary mail of its own
principal: the rest waits for a run under that one. Mail reaching a member that already ended is
refused under its writer. Reference: spec/tools/fixtures/ops_consume.py.

Applying a cancel is lane 21E.2's: until then a cancel stays pending, and it stops the ordinary
mail behind it (a member being cancelled takes no new work)."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, assert_never

from threads.log import Event, MailEnvelope, MemberEndedEvent, ParkAddress, Principal, Provenance
from threads.log.keys import principal_key
from threads.reduce.fold import loop_parked
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.close import CloseContext, take_answer, take_wait_notice
from threads.team.mail import received
from threads.team.park import take_park_notice
from threads.team.provenance import turn_provenance
from threads.team.rows import MemberRow, own_rows, pending_for, team_row
from threads.team.settle import refuse_all
from threads.team.view import parked_on

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class ConsumeContext(CloseContext):
    principal: Principal | None = None
    """A member run's principal: ordinary mail of another waits for a run under that one."""


@dataclass(frozen=True, slots=True)
class Consumed:
    status: Literal["nothing_pending", "consumed", "refused"]
    mail_ids: tuple[str, ...] = ()


def _pair(provenance: Provenance) -> str:
    """One turn's authority and budget root, as a key."""
    root = provenance.root_request
    return f"{principal_key(provenance.principal)} {root.thread_id}:{root.event_id}"


@dataclass(slots=True)
class _Pass:
    turn: str | None
    blocked: bool
    batch: str | None = None
    taken: list[str] = field(default_factory=list[str])


def consumable(env: MailEnvelope) -> bool:
    """Mail a consume can take now, so the worker wakes its recipient for it: all but a cancel,
    which lane 21E.2 applies."""
    return env.kind != "cancel"


def may_resume(env: MailEnvelope) -> bool:
    """Mail that may be control mail for a parked recipient (an answer, a notice, a park notice),
    so the worker wakes a parked member for it. Its consume decides."""
    answers = env.kind == "bounce" and isinstance(env.ask_id, str)
    return answers or env.kind in ("reply", "member_settled", "member_ended", "member_parked")


def consume(ctx: ConsumeContext) -> Consumed:
    """Consumes this writer's pending mail into the batch."""
    rows = own_rows(ctx.conn, ctx.thread_id)
    pending = pending_for(ctx.conn, rows)
    if not pending:
        return Consumed("nothing_pending")
    ended = next((r for r in rows if r.state == "ended"), None)
    if ended is not None:
        return _refuse_ended(ctx, ended)
    fold = ctx.fold
    opened = turn_provenance(ctx.conn, fold.events) if fold.in_turn else None
    turn = None if opened is None else _pair(Provenance.model_validate(opened))
    state = _Pass(turn, bool(loop_parked(fold)))
    for env in pending:
        if _take(ctx, state, env):
            state.taken.append(env.mail_id)
    return Consumed("consumed", tuple(state.taken))


def _take(ctx: ConsumeContext, state: _Pass, env: MailEnvelope) -> bool:
    """Whether this pass takes the row: as control mail, or into its one ordinary batch."""
    # An earlier control row of this pass took it (an ask's reply, a wait's notice).
    if env.mail_id in ctx.batch.taken():
        return True
    control = _control(ctx, env)
    if control == "later":
        state.blocked = True
        return False
    if control == "taken":
        return True
    if control == "park":
        # Ordinary mail behind a new park waits for the next consume.
        if take_park_notice(ctx, env, team_log=False):
            state.blocked = True
        return True
    return not state.blocked and _ordinary(ctx, state, env)


def _ordinary(ctx: ConsumeContext, state: _Pass, env: MailEnvelope) -> bool:
    """Ordinary mail: into the pass's one batch of one (principal, root_request)."""
    pair = _pair(env.provenance)
    if state.turn is not None:
        if pair != state.turn:
            return False
    elif (state.batch is not None and pair != state.batch) or not _under_run(ctx, env):
        state.blocked = True
        return False
    state.batch = pair
    ctx.batch.add(received(env))
    return True


def _under_run(ctx: ConsumeContext, env: MailEnvelope) -> bool:
    who = ctx.principal
    return who is None or principal_key(who) == principal_key(env.provenance.principal)


def _control(
    ctx: ConsumeContext, env: MailEnvelope
) -> Literal["taken", "park", "later", "ordinary"]:
    """Control mail, the exhaustive list, applied now ("taken"; a park notice is "park"): a reply
    or an ask's bounce, a wait's notice, and a task or end notice resolving a `{kind: member}`
    park. A cancel waits for lane 21E.2 ("later"); anything else is ordinary."""
    kind: Literal["taken", "park", "later", "ordinary"]
    match env.kind:
        case "member_parked":
            kind = "park"
        case "cancel":
            kind = "later"
        case "reply":
            take_answer(ctx, env)
            kind = "taken"
        case "bounce" if isinstance(env.ask_id, str):
            take_answer(ctx, env)
            kind = "taken"
        case "member_settled" | "member_ended":
            kind = "taken" if _take_notice(ctx, env) else "ordinary"
        case "bounce" | "message" | "ask" | "task":
            kind = "ordinary"
        case _:
            assert_never(env.kind)
    return kind


def _take_notice(ctx: ConsumeContext, env: MailEnvelope) -> bool:
    """A settle or end notice: a wait's, or a task or end notice resolving the park on it."""
    if take_wait_notice(ctx, env):
        return True
    monitor = env.monitor_id if isinstance(env.monitor_id, str) else ""
    address = ParkAddress(kind="member", id=monitor)
    if not parked_on(ctx.fold, ctx.batch, address):
        return False
    got = ctx.batch.add(received(env))
    data: dict[str, JsonValue] = {"address": to_json(address), "cause_event_id": got}
    ctx.batch.add(Draft("resumed", data))
    return True


def _refuse_ended(ctx: ConsumeContext, row: MemberRow) -> Consumed:
    """An ended member refuses what still reaches it; only a message or an ask is bounced."""
    team = team_row(ctx.conn, row.team_id)
    last = _last_end(ctx.fold.events)
    if team is None or last is None:
        raise AssertionError("an ended member's log records its member_ended")
    result = to_json(last.data.result)
    refused = refuse_all(ctx, team, row, result, bounce_all=False)
    return Consumed("refused", tuple(m.mail_id for m in refused))


def _last_end(events: Sequence[Event]) -> MemberEndedEvent | None:
    return next((e for e in reversed(events) if isinstance(e, MemberEndedEvent)), None)
