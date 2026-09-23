import { storeConnection } from "@threads/core/host";
import type { HostContext } from "../context";
import { occurrences } from "../cron";
import type { Bound } from "./bind";
import { decideThread, type Pass } from "./decide";
import {
  lastOccurrence,
  pendingRows,
  reserveDue,
  scheduleThreads,
} from "./rows";

// A scheduler pass. A schedule runs on one thread while its agent's config is unchanged; an
// occurrence is (tenant, schedule id, scheduled instant UTC). A due occurrence is first reserved
// as a pending row, with the agent, input and timezone it fires with frozen, and then decided
// under its thread's writer. Every pass decides all of the tenant's pending rows, whatever
// schedules are configured now, so a reservation outlives a restart or its schedule's removal.

const LOCAL_TENANT = "local";
/** Enough to see both instances of a fall-back hour, so its second one is never run. */
const LOOKBACK_MS = 3 * 3_600_000;

/** One scheduler pass at `now`; `startedAt` is when this host became ready. */
export async function tick(
  ctx: HostContext,
  bound: readonly Bound[],
  startedAt: number,
  now: number,
  tenant: string = LOCAL_TENANT,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  const { log } = await ctx.open(tenant);
  const pass: Pass = { ctx, db, log, tenant };
  for (const b of bound) await reserve(pass, b, startedAt, now);
  // Reserved first, so a thread's backlog and its newly due occurrences are decided in one
  // writer hold, in occurrence order.
  const threads = new Set(pendingRows(db, tenant).map((r) => r.thread_id));
  for (const thread of threads) await decideThread(pass, thread);
  // A run whose input is durable but that never went (its host died, or the writer was held
  // when it fired) runs on from the log (found by the F10.5 drill).
  for (const id of scheduleThreads(db, tenant)) {
    const main = log.mainBranch(id);
    // ponytail: a run in flight here gets a no-op resume queued behind it each tick; track
    // in-flight branches if ticks ever outpace runs.
    if (main.ok) void ctx.recover(tenant, { id, branch: main.value });
  }
}

/** Reserves the schedule's occurrences due since its last one (or since ready). */
async function reserve(
  pass: Pass,
  b: Bound,
  startedAt: number,
  now: number,
): Promise<void> {
  const { db, log, tenant } = pass;
  const id = b.schedule.id;
  const after = lastOccurrence(db, tenant, id) ?? startedAt;
  const due = occurrences(b.cron, b.timezone, after - LOOKBACK_MS, now).filter(
    (at) => at > after,
  );
  if (due.length === 0) return;
  reserveDue(
    db,
    log,
    await b.hosted.runner.started(),
    due.map((at) => ({
      schedule_id: id,
      occurrence_at: at,
      agent: b.hosted.key,
      input: b.schedule.input,
      timezone: b.timezone,
      missed: at <= startedAt,
    })),
  );
}
