import {
  addressed,
  type CallContext,
  callerOf,
  callMailId,
  decide,
  isRefusal,
  type Refused,
  recorded,
} from "./call";
import { finishWait, publicResult, type Waiting } from "./close";
import { TEAM_CONSTANTS } from "./constants";
import { parkCall } from "./park";
import type { MonitorResult, Waited, Wire } from "./results";
import { type MemberRow, refOf, settledOf } from "./rows";

// The model tools wait and monitor (spec/schema/README.md, "Teams", Waits and monitors; design
// §4.12 and §4.13). Each reads its targets' rows in its own transaction: a settled target is
// recorded as member_observed (its committed result, copied), any other gets a monitor row that
// its idle or end append fires. Never both, never neither. Reference:
// spec/tools/fixtures/ops_observe.py.

const SETTLED: ReadonlySet<MemberRow["state"]> = new Set(["idle", "ended"]);

/** member_observed: the target's committed result, and where it is. */
async function observe(
  ctx: CallContext,
  monitorId: string,
  row: MemberRow,
): Promise<void> {
  if (row.branch_id === null) throw new Error("a settled member has a branch");
  const { result, seq } = await settledOf(ctx.tx, row);
  ctx.batch.add({
    type: "member_observed",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      monitor_id: monitorId,
      result,
      source: { thread_id: row.thread_id, branch_id: row.branch_id, seq },
    },
  });
}

/**
 * wait (mode all, the default deadline): the listed members, a repeat dropped, each known at its
 * generation, then one monitor decision each; wait_started, an observation per settled member,
 * and the finish when that meets the mode, else a park. A re-dispatched wait only parks.
 */
export async function wait(
  ctx: CallContext,
  args: { readonly members: readonly string[] },
  timeoutMs: number = TEAM_CONSTANTS.askWaitDefaultMs,
): Promise<Wire<Waited> | Waiting | Refused> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  const waitId = callMailId(ctx);
  const again = ctx.chain.events.some(
    (l) =>
      l.kind === "event" &&
      l.event.type === "wait_started" &&
      l.event.data.wait_id === waitId,
  );
  if (again) {
    await parkCall(ctx, caller);
    return { status: "waiting", wait_id: waitId };
  }
  const rows: MemberRow[] = [];
  for (const name of new Set(args.members)) {
    const row = await addressed(ctx, caller, name);
    if (isRefusal(row)) return recorded(ctx, row);
    rows.push(row);
  }
  for (const row of rows) decide(ctx, "monitor", row.name, true);
  const cap = TEAM_CONSTANTS.askWaitDefaultMs;
  const started = ctx.batch.add({
    type: "wait_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      wait_id: waitId,
      members: rows.map((r) => refOf(caller.team, r)),
      mode: "all",
      deadline: ctx.batch.now + Math.min(timeoutMs, cap),
    },
  });
  const settled = rows.filter((r) => SETTLED.has(r.state));
  for (const row of settled)
    await observe(ctx, `${ctx.call.branch_id}:${started}:${row.name}`, row);
  if (settled.length === rows.length)
    return await finishWait(closing(ctx), waitId, { deadline: false });
  await parkCall(ctx, caller);
  return { status: "waiting", wait_id: waitId };
}

/** The call's append as a close: the caller's own thread and branch. */
function closing(ctx: CallContext): Parameters<typeof finishWait>[0] {
  return {
    ...ctx,
    threadId: ctx.call.thread_id,
    branchId: ctx.call.branch_id,
  };
}

/**
 * monitor: its decision, then the member; monitor_set, and for an ended target its observation
 * and the ended result at once, else an end monitor row whose firing opens a turn when idle.
 */
export async function monitor(
  ctx: CallContext,
  args: { readonly member: string },
): Promise<Wire<MonitorResult> | Refused> {
  const caller = await callerOf(ctx);
  if (caller === undefined) throw new Error("a team tool call outside a team");
  decide(ctx, "monitor", args.member, true);
  const row = await addressed(ctx, caller, args.member);
  if (isRefusal(row)) return recorded(ctx, row);
  const member = refOf(caller.team, row);
  const set = ctx.batch.add({
    type: "monitor_set",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: { member },
  });
  if (row.state !== "ended")
    return recorded(ctx, {
      member,
      status: "monitoring",
    } satisfies Wire<MonitorResult>);
  await observe(ctx, `${ctx.call.branch_id}:${set}:${row.name}`, row);
  const { result } = await settledOf(ctx.tx, row);
  return recorded(ctx, {
    result: await publicResult(result, ctx.read),
    status: "ended",
  } satisfies Wire<MonitorResult>);
}
