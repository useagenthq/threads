"""mail.consume under the recipient's writer (spec/schema/README.md, "Teams"; design §4.7): the
pending rows in (created_at, mail_id) order. Control mail is taken whatever its provenance;
ordinary mail only when the recipient was not parked when the consume began, as one batch of one
(principal, root_request): mid-turn the open turn's, else the first row's, ending at the first row
of another. A member run takes only ordinary mail of its own principal: the rest waits for a
run under that one. Mail reaching a member that already ended is refused under its writer.
Reference: spec/tools/fixtures/ops_consume.py.

Lane 21E adds the rest of control mail: applying a cancel, the replies, bounces and notices that
complete an ask or a wait. Until then those stay pending, and a pending cancel stops the
ordinary mail behind it: a member being cancelled takes no new work."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from threads.log import Event, MailEnvelope, MemberEndedEvent, Principal, Provenance
from threads.log.keys import principal_key
from threads.reduce import Fold
from threads.reduce.fold import loop_parked
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.mail import received
from threads.team.park import take_park_notice
from threads.team.provenance import turn_provenance
from threads.team.rows import MemberRow, own_rows, pending_for, team_row
from threads.team.settle import AppendContext, refuse_all

if TYPE_CHECKING:
    from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class ConsumeContext(AppendContext):
    fold: Fold
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
    """Mail a consume can take now, so the worker wakes its recipient for it: all but the
    control mail lane 21E consumes (a cancel, a reply, an ask's bounce)."""
    later = env.kind in ("cancel", "reply")
    return not (later or (env.kind == "bounce" and isinstance(env.ask_id, str)))


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
        if _take(ctx, fold, state, env):
            state.taken.append(env.mail_id)
    return Consumed("consumed", tuple(state.taken))


def _take(ctx: ConsumeContext, fold: Fold, state: _Pass, env: MailEnvelope) -> bool:
    """Whether this pass takes the row: as control mail, or into its one ordinary batch."""
    control = _control(fold, env)
    if control == "later":
        state.blocked = state.blocked or env.kind == "cancel"
        return False
    if control != "ordinary":
        _take_control(ctx, state, env, control)
        return True
    if state.blocked:
        return False
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


def _take_control(
    ctx: ConsumeContext, state: _Pass, env: MailEnvelope, control: Literal["resume", "park"]
) -> None:
    if control == "resume":
        _resume(ctx, env)
    elif take_park_notice(ctx, env, team_log=False):
        # Ordinary mail behind a new park waits for the next consume.
        state.blocked = True


def _resume(ctx: ConsumeContext, env: MailEnvelope) -> None:
    """A task or end notice resolving the park on it: its receipt, then resumed."""
    got = ctx.batch.add(received(env))
    monitor = env.monitor_id if isinstance(env.monitor_id, str) else ""
    data: dict[str, JsonValue] = {
        "address": {"kind": "member", "id": monitor},
        "cause_event_id": got,
    }
    ctx.batch.add(Draft("resumed", data))


def _control(fold: Fold, env: MailEnvelope) -> Literal["resume", "park", "later", "ordinary"]:
    """Control mail this lane takes: a member's park notice, and a task or end notice resolving a
    `{kind: member}` park. The rest of control mail waits for lane 21E ("later"); anything else
    is ordinary."""
    if env.kind == "member_parked":
        return "park"
    if not consumable(env):
        return "later"
    if env.kind not in ("member_settled", "member_ended"):
        return "ordinary"
    monitor = env.monitor_id
    if isinstance(monitor, str) and monitor in fold.team.settle:
        return "later"
    parked = any(p.kind == "member" and p.id == monitor for p in fold.parked)
    return "resume" if parked else "ordinary"


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
