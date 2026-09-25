import type { EventOf } from "../fold/state";
import type { Chain } from "../verify";
import { applyCancel } from "./cancel";
import {
  type CloseContext,
  committedNotices,
  completeAsk,
  finishWait,
  mine,
  type Waiting,
} from "./close";
import type { AskOutcome, Waited, Wire } from "./results";
import { askRow, dueAsks, teamOfLog } from "./rows";
import { askOpen, openWaits } from "./view";

// The team worker's deadline step for one ask or wait, under the asker's or waiter's writer
// (spec/schema/README.md, "Teams"; design §4.8 and §4.12): it runs at or after the deadline, as
// one transaction. A wait counts every settlement already committed (commit order, never
// timestamps: a settlement counts iff its append deleted the monitor row first); an ask still
// answers a reply that committed before its deadline. Reference: ops_consume.py, deadline.

const NOT_DUE = { status: "not_due" } as const;

function startedOf(
  chain: Chain,
  waitId: string,
): EventOf<"wait_started"> | undefined {
  for (const l of chain.events)
    if (
      l.kind === "event" &&
      l.event.type === "wait_started" &&
      l.event.data.wait_id === waitId
    )
      return l.event;
  return undefined;
}

/** What a deadline step did: closed the ask, finished the wait, or nothing yet. */
export type DeadlineOutcome =
  | Wire<AskOutcome>
  | Wire<Waited>
  | Waiting
  | { readonly wait_id: string; readonly status: "cancelled" }
  | typeof NOT_DUE;

/**
 * The deadline step for `id`, an AskId or a WaitId of this writer. A cancel pending for the
 * writer is applied here too, as a consume would: its barrier closes the ask or finishes the wait.
 */
export async function deadline(
  ctx: CloseContext,
  id: string,
): Promise<DeadlineOutcome> {
  const ask = await askRow(ctx.tx, id);
  if (ask !== undefined) return askDue(ctx, id, ask.deadline);
  const started = startedOf(ctx.chain, id);
  const due =
    started !== undefined &&
    ctx.batch.now >= started.data.deadline &&
    openWaits(ctx.chain, ctx.batch).has(id);
  if (!due) return NOT_DUE;
  const cancel = (await mine(ctx)).find((m) => m.kind === "cancel");
  if (cancel !== undefined) {
    await applyCancel(ctx, cancel);
    return { wait_id: id, status: "cancelled" };
  }
  await committedNotices(ctx, id);
  return await finishWait(ctx, id, { deadline: true });
}

async function askDue(
  ctx: CloseContext,
  id: string,
  due: number,
): Promise<DeadlineOutcome> {
  const open = await askOpen(ctx.tx, ctx.branchId, id, ctx.batch);
  if (!open) return NOT_DUE;
  // Team close is a trigger of its own: the team log's next step closes its open asks.
  const closed = (await teamOfLog(ctx.tx, ctx.branchId))?.closed_at ?? null;
  if (closed !== null)
    return (
      (await completeAsk(ctx, id, { cancelled: true, due: false })) ?? NOT_DUE
    );
  if (ctx.batch.now < due) return NOT_DUE;
  const pending = await mine(ctx);
  const answered = pending.some(
    (m) => (m.kind === "reply" || m.kind === "bounce") && m.ask_id === id,
  );
  const cancel = pending.find((m) => m.kind === "cancel");
  if (cancel !== undefined && !answered) {
    // The barrier closes the ask cancelled, not timed out.
    await applyCancel(ctx, cancel);
    return { ask_id: id, status: "cancelled" };
  }
  return (
    (await completeAsk(ctx, id, { cancelled: false, due: true })) ?? NOT_DUE
  );
}

/** This writer's asks and waits whose deadline has passed by `now`. */
export async function dueIds(
  ctx: Pick<CloseContext, "tx" | "chain" | "branchId">,
  now: number,
): Promise<readonly string[]> {
  const waits = [...ctx.chain.fold.team.waits].filter((id) => {
    const started = startedOf(ctx.chain, id);
    return started !== undefined && now >= started.data.deadline;
  });
  return [...(await dueAsks(ctx.tx, ctx.branchId, now)), ...waits];
}

/** The earliest deadline among this writer's open asks and waits, if any. */
export async function nextDeadline(
  ctx: Pick<CloseContext, "tx" | "chain" | "branchId">,
): Promise<number | undefined> {
  const asks: number[] = [];
  for (const id of await dueAsks(ctx.tx, ctx.branchId, Number.MAX_SAFE_INTEGER))
    asks.push((await askRow(ctx.tx, id))?.deadline ?? Number.MAX_SAFE_INTEGER);
  const waits = [...ctx.chain.fold.team.waits].map((id) => {
    const started = startedOf(ctx.chain, id);
    return started !== undefined
      ? started.data.deadline
      : Number.MAX_SAFE_INTEGER;
  });
  const all = [...asks, ...waits];
  return all.length === 0 ? undefined : Math.min(...all);
}
