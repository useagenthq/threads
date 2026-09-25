"""A cancel for a starting member (spec/schema/README.md, "Teams", "A cancel for a starting
member"): its materialize never tries the rebind, so a member whose setup can't succeed can still
be cancelled. After the branch's thread_started and task input, the same append takes the cancel,
closes the task turn as the cancellation step does, and ends the member cancelled. Reference:
spec/tools/fixtures/ops_start.py (materialize, _cancelled). Mirrors TypeScript's
team/cancel-start.ts."""

from pydantic import JsonValue

from threads.log import MailEnvelope
from threads.reduce.handlers import to_json
from threads.store.lines import Draft
from threads.team.mail import received
from threads.team.settle import AppendContext, SettleContext, settle


def start_cancelled(ctx: AppendContext, task: MailEnvelope, cancel: MailEnvelope) -> None:
    """The cancel's receipt, the barrier, the turn's cancelled end, and member_ended{cancelled}."""
    ctx.batch.add(received(cancel))
    actor: dict[str, JsonValue] = {
        "kind": "host",
        "principal": to_json(cancel.provenance.principal),
    }
    barrier = ctx.batch.add(Draft("cancel_requested", {"scope": "tree"}, actor))
    ctx.batch.add(Draft("cancelled", {"request_event_id": barrier}))
    ctx.batch.add(Draft("turn_completed", {"reason": "cancelled"}))
    at = SettleContext(
        ctx.conn, ctx.batch, ctx.thread_id, ctx.branch_id, to_json(task.provenance), _no_text
    )
    settle(at, {"status": "cancelled"})


def _no_text(_text: str) -> JsonValue:
    raise AssertionError("a cancelled start's result has no text")
