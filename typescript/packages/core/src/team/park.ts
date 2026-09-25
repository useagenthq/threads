import { type EventOf, loopPending, type ParkAddress } from "../fold/state";
import type { MailEnvelope } from "../log";
import type { CallContext, Caller } from "./call";
import { addressOf, received, sent } from "./mail";
import { monitorsOn, ownRows, refOf, teamRow } from "./rows";
import type { AppendContext, SettleContext } from "./settle";
import { type Item, itemsOf, openWaits, parkedOn } from "./view";

// A member's park and its starter (spec/schema/README.md, "Teams"; design §2.7.1): the member's
// first park while its task monitor is unfired carries one member_parked notice to the starter,
// in the same append. The notice is control mail: the starter parks on the member while that
// task monitor still exists (the team log records the receipt only), and a stale notice creates
// no park. Reference: spec/tools/fixtures/ops_request.py (park) and ops_consume.py.

type ParkReason = EventOf<"parked">["data"]["reason"];

/** The one member_parked notice of a member's first park, after the parked draft. */
export async function parkNotice(
  ctx: Omit<SettleContext, "put">,
  parked: { readonly eventId: string; readonly reason: ParkReason },
): Promise<void> {
  const row = (await ownRows(ctx.tx, ctx.threadId)).find(
    (r) => r.role === "member",
  );
  if (row === undefined) return;
  const task = (await monitorsOn(ctx.tx, row.team_id, row)).find(
    (m) => m.kind === "task",
  );
  const team = await teamRow(ctx.tx, row.team_id);
  if (task === undefined || team === undefined) return;
  ctx.batch.add(
    sent({
      mail_id: `${ctx.branchId}:${ctx.batch.nextId()}`,
      kind: "member_parked",
      team: row.team_id,
      from: refOf(team, row),
      to: await addressOf(ctx.tx, row.team_id, task.watcher_branch_id),
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
export async function takeParkNotice(
  ctx: AppendContext,
  env: MailEnvelope,
  teamLog: boolean,
): Promise<boolean> {
  const monitor = env.monitor_id ?? "";
  const live =
    (await (
      await ctx.tx.all("SELECT 1 FROM monitors WHERE monitor_id = ?", [monitor])
    ).length) > 0;
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

/**
 * A member's ask or wait parks its turn once the turn has nothing else to run: every pending call
 * is an opened ask or wait of this log (a call the batch closes no longer counts). Each gets one
 * park, and the member's first park carries its member_parked notice. The team log never parks.
 */
export async function parkCall(
  ctx: CallContext,
  caller: Caller,
): Promise<void> {
  const addresses = waitingOn(ctx);
  if (addresses === undefined) return;
  let first = !itemsOf(ctx.chain, ctx.batch).some((e) => e.type === "parked");
  for (const address of addresses) {
    if (parkedOn(ctx.chain, ctx.batch, address)) continue;
    const eventId = ctx.batch.add({
      type: "parked",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: { address, reason: "awaiting_member" },
    });
    if (!first) continue;
    first = false;
    await parkNotice(
      {
        tx: ctx.tx,
        batch: ctx.batch,
        threadId: ctx.call.thread_id,
        branchId: ctx.call.branch_id,
        provenance: caller.provenance,
      },
      { eventId, reason: "awaiting_member" },
    );
  }
}

/** Each pending call's ask or wait, or undefined when a pending call is still runnable. */
function waitingOn(ctx: CallContext): readonly ParkAddress[] | undefined {
  const items = itemsOf(ctx.chain, ctx.batch);
  const waits = openWaits(ctx.chain, ctx.batch);
  const out: ParkAddress[] = [];
  for (const callId of loopPending(ctx.chain.fold)) {
    const id = `${ctx.call.branch_id}:${callId}`;
    const closed = items.some(
      (e) => e.type === "tool_result" && e.data.call_id === callId,
    );
    if (closed) continue;
    if (waits.has(id)) out.push({ kind: "wait", id });
    else if (items.some((e) => isAsk(e, id))) out.push({ kind: "ask", id });
    else return undefined;
  }
  return out;
}

const isAsk = (e: Item, id: string): boolean =>
  e.type === "message_sent" &&
  e.data.envelope.kind === "ask" &&
  e.data.envelope.mail_id === id;
