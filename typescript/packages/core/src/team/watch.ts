import type { MemberRef } from "../log";
import {
  addressed,
  type CallContext,
  callerOf,
  callMailId,
  callRequest,
  decide,
  decision,
  isRefusal,
  named,
  type Refused,
  recorded,
} from "./call";
import {
  type CloseContext,
  finishWait,
  publicResult,
  type Waiting,
} from "./close";
import { TEAM_CONSTANTS } from "./constants";
import { parkCall } from "./park";
import type { Request, Target } from "./request";
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
  ctx: Pick<CloseContext, "tx" | "batch">,
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

/** How many listed members must settle: all, any (one), or a count. */
export type WaitMode = "all" | "any" | number;

/**
 * The model's wait (mode all, the default deadline): the listed members, a repeat dropped, then
 * the wait. A re-dispatched wait only parks, and so does one left waiting.
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
  if (!again) {
    const targets = [...new Set(args.members)].map((m) => named(ctx, m));
    const got = await openWait(await callRequest(ctx), closing(ctx), targets, {
      mode: "all",
      timeoutMs,
    });
    if (got.status !== "waiting") return got;
  }
  await parkCall(ctx, caller);
  return { status: "waiting", wait_id: waitId };
}

/**
 * An operator wait's members, a repeat dropped (they are frozen at the call); invalid_request for
 * an empty list, or a numeric mode below 1 or above their count, refused before any writer is
 * taken.
 */
export function waitMembers(
  members: readonly MemberRef[],
  mode: WaitMode | undefined,
): readonly MemberRef[] | "invalid_request" {
  const key = (m: MemberRef) =>
    JSON.stringify([m.tenant, m.team, m.name, m.generation]);
  const distinct = [...new Map(members.map((m) => [key(m), m])).values()];
  const bad =
    distinct.length === 0 ||
    (typeof mode === "number" && (mode < 1 || mode > distinct.length));
  return bad ? "invalid_request" : distinct;
}

/**
 * A wait, for a model call or an operator request: each target known at its generation, then one
 * monitor decision each; wait_started, an observation per settled member, and the finish when
 * that meets the mode. Returns what it recorded, or waiting.
 */
export async function openWait(
  req: Request,
  close: CloseContext,
  targets: readonly Target[],
  how: { readonly mode: WaitMode; readonly timeoutMs: number },
): Promise<Wire<Waited> | Waiting | Refused> {
  const rows: MemberRow[] = [];
  for (const target of targets) {
    const row = await target.row();
    if ("refused" in row) return req.refuse(row);
    rows.push(row);
  }
  for (const row of rows) {
    const denied = req.decide("monitor", row.name);
    if (denied !== undefined) return req.refuse(denied);
  }
  const cap = TEAM_CONSTANTS.askWaitDefaultMs;
  const started = req.batch.add({
    type: "wait_started",
    type_version: 1,
    critical: true,
    actor: { kind: "host" },
    data: {
      wait_id: req.mailId,
      members: rows.map((r) => refOf(req.team, r)),
      mode: how.mode,
      deadline: req.batch.now + Math.min(how.timeoutMs, cap),
    },
  });
  for (const row of rows)
    if (SETTLED.has(row.state))
      await observe(close, `${close.branchId}:${started}:${row.name}`, row);
  return await finishWait(close, req.mailId, { deadline: false });
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
  const denied = decide(
    ctx,
    "monitor",
    args.member,
    decision(ctx, caller, "monitor", args.member),
  );
  if (denied !== undefined) return recorded(ctx, denied);
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
