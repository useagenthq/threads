"""A member's park and its starter (spec/schema/README.md, "Teams"; design §2.7.1): the member's
first park while its task monitor is unfired carries one member_parked notice to the starter, in
the same append. The notice is control mail: the starter parks on the member while that task
monitor still exists (the team log records the receipt only), and a stale notice creates no park.
Reference: spec/tools/fixtures/ops_request.py (park) and ops_consume.py."""

from pydantic import JsonValue

from threads.log import MailEnvelope
from threads.store.lines import Draft
from threads.team.mail import address_of, received, sent
from threads.team.rows import monitors_on, own_rows, ref_of, team_row
from threads.team.settle import AppendContext


def park_notice(ctx: AppendContext, provenance: JsonValue, event_id: str, reason: str) -> None:
    """The one member_parked notice of a member's first park, after the parked draft."""
    row = next((r for r in own_rows(ctx.conn, ctx.thread_id) if r.role == "member"), None)
    if row is None:
        return
    task = next((m for m in monitors_on(ctx.conn, row.team_id, row) if m.kind == "task"), None)
    team = team_row(ctx.conn, row.team_id)
    if task is None or team is None:
        return
    env: dict[str, JsonValue] = {
        "mail_id": f"{ctx.branch_id}:{ctx.batch.next_id()}",
        "kind": "member_parked",
        "team": row.team_id,
        "from": ref_of(team, row),
        "to": address_of(ctx.conn, row.team_id, task.watcher_branch_id),
        "provenance": provenance,
        "causal": {"thread_id": ctx.thread_id, "event_id": event_id},
        "monitor_id": task.monitor_id,
        "reason": reason,
    }
    ctx.batch.add(sent(env))


def take_park_notice(ctx: AppendContext, env: MailEnvelope, *, team_log: bool) -> bool:
    """The starter takes a member_parked notice as control mail: its receipt, and a park on the
    member while the task monitor it names is still live. The team log never parks."""
    monitor = env.monitor_id if isinstance(env.monitor_id, str) else ""
    live = ctx.conn.execute("SELECT 1 FROM monitors WHERE monitor_id = ?", (monitor,)).fetchone()
    ctx.batch.add(received(env))
    if live is None or team_log:
        return False
    address: JsonValue = {"kind": "member", "id": monitor}
    ctx.batch.add(Draft("parked", {"address": address, "reason": "awaiting_member"}))
    return True
