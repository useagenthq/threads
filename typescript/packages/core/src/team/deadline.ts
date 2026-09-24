import type { EventOf } from "../fold/state";
import type { Chain } from "../verify";
import {
  type CloseContext,
  committedNotices,
  completeAsk,
  finishWait,
} from "./close";
import { askRow, dueAsks } from "./rows";
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

/** The deadline step for `id`, an AskId or a WaitId of this writer. */
export function deadline(ctx: CloseContext, id: string): unknown {
  const ask = askRow(ctx.db, id);
  if (ask !== undefined) {
    const open = askOpen(ctx.db, ctx.branchId, id, ctx.batch);
    if (!open || ctx.batch.now < ask.deadline) return NOT_DUE;
    return completeAsk(ctx, id, { cancelled: false, due: true }) ?? NOT_DUE;
  }
  const started = startedOf(ctx.chain, id);
  const due =
    started !== undefined &&
    ctx.batch.now >= started.data.deadline &&
    openWaits(ctx.chain, ctx.batch).has(id);
  if (!due) return NOT_DUE;
  committedNotices(ctx, id);
  return finishWait(ctx, id, { deadline: true });
}

/** This writer's asks and waits whose deadline has passed by `now`. */
export function dueIds(
  ctx: Pick<CloseContext, "db" | "chain" | "branchId">,
  now: number,
): readonly string[] {
  const waits = [...ctx.chain.fold.team.waits].filter((id) => {
    const started = startedOf(ctx.chain, id);
    return started !== undefined && now >= started.data.deadline;
  });
  return [...dueAsks(ctx.db, ctx.branchId, now), ...waits];
}

/** The earliest deadline among this writer's open asks and waits, if any. */
export function nextDeadline(
  ctx: Pick<CloseContext, "db" | "chain" | "branchId">,
): number | undefined {
  const asks = dueAsks(ctx.db, ctx.branchId, Number.MAX_SAFE_INTEGER).map(
    (id) => askRow(ctx.db, id)?.deadline ?? Number.MAX_SAFE_INTEGER,
  );
  const waits = [...ctx.chain.fold.team.waits].map((id) => {
    const started = startedOf(ctx.chain, id);
    return started !== undefined
      ? started.data.deadline
      : Number.MAX_SAFE_INTEGER;
  });
  const all = [...asks, ...waits];
  return all.length === 0 ? undefined : Math.min(...all);
}
