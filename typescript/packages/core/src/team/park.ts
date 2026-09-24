import type { EventOf, ParkAddress } from "../fold/state";
import type { MailEnvelope } from "../log";
import { addressOf, received, sent } from "./mail";
import { monitorsOn, ownRows, refOf, teamRow } from "./rows";
import type { AppendContext, SettleContext } from "./settle";

// A member's park and its starter (spec/schema/README.md, "Teams"; design §2.7.1): the member's
// first park while its task monitor is unfired carries one member_parked notice to the starter,
// in the same append. The notice is control mail: the starter parks on the member while that
// task monitor still exists (the team log records the receipt only), and a stale notice creates
// no park. Reference: spec/tools/fixtures/ops_request.py (park) and ops_consume.py.

type ParkReason = EventOf<"parked">["data"]["reason"];

/** The one member_parked notice of a member's first park, after the parked draft. */
export function parkNotice(
  ctx: Omit<SettleContext, "put">,
  parked: { readonly eventId: string; readonly reason: ParkReason },
): void {
  const row = ownRows(ctx.db, ctx.threadId).find((r) => r.role === "member");
  if (row === undefined) return;
  const task = monitorsOn(ctx.db, row.team_id, row).find(
    (m) => m.kind === "task",
  );
  const team = teamRow(ctx.db, row.team_id);
  if (task === undefined || team === undefined) return;
  ctx.batch.add(
    sent({
      mail_id: `${ctx.branchId}:${ctx.batch.nextId()}`,
      kind: "member_parked",
      team: row.team_id,
      from: refOf(team, row),
      to: addressOf(ctx.db, row.team_id, task.watcher_branch_id),
      provenance: ctx.provenance,
      causal: { thread_id: ctx.threadId, event_id: parked.eventId },
      monitor_id: task.monitor_id,
      reason: parked.reason,
    }),
  );
}

/**
 * The starter takes a member_parked notice as control mail: its receipt, and a park on the
 * member while the task monitor it names is still live. The team log never parks.
 */
export function takeParkNotice(
  ctx: AppendContext,
  env: MailEnvelope,
  teamLog: boolean,
): boolean {
  const monitor = env.monitor_id ?? "";
  const live =
    ctx.db.all("SELECT 1 FROM monitors WHERE monitor_id = ?", [monitor])
      .length > 0;
  ctx.batch.add(received(env));
  if (!live || teamLog) return false;
  const address: ParkAddress = { kind: "member", id: monitor };
  ctx.batch.add({
    type: "parked",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { address, reason: "awaiting_member" },
  });
  return true;
}
