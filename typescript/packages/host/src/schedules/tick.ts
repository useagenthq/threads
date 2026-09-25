import { READ_ONLY, storeConnection } from "@threads/core/host";
import type { HostContext } from "../context";
import { occurrences } from "../cron";
import { isolated } from "../isolated";
import { Recovery } from "../recovery";
import type { Bound } from "./bind";
import { decideThread } from "./decide";
import { reserveDue } from "./identity";
import { newPass, type Pass } from "./pass";
import { lastOccurrence, pendingRows, scheduleThreads } from "./rows";

// A scheduler pass. A schedule runs on one thread while its agent's config is unchanged; an
// occurrence is (tenant, schedule id, scheduled instant UTC). A due occurrence is first reserved
// as a pending row, with the agent, input and timezone it fires with frozen, and then decided
// under its thread's writer. Every pass decides all of the tenant's pending rows, whatever
// schedules are configured now, so a reservation outlives a restart or its schedule's removal.
// Each schedule's reservation and each thread's decisions fail alone, and recovery always runs.

const LOCAL_TENANT = "local";
/** Enough to see both instances of a fall-back hour, so its second one is never run. */
const LOOKBACK_MS = 3 * 3_600_000;

/**
 * One scheduler pass at `now`; `startedAt` is when this host became ready. `recovery` is the
 * host's own, so a thread's store-outage backoff carries across ticks.
 */
export async function tick(
  ctx: HostContext,
  bound: readonly Bound[],
  startedAt: number,
  now: number,
  recovery: Recovery = new Recovery(ctx),
  tenant: string = LOCAL_TENANT,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  const { log } = await ctx.open(tenant);
  const pass = newPass(ctx, db, log, tenant);
  for (const b of bound)
    await isolated(`schedule ${b.schedule.id}`, () =>
      reserve(pass, b, startedAt, now),
    );
  // Reserved first, so a thread's backlog and its newly due occurrences are decided in one
  // writer hold, in occurrence order.
  await isolated("the pending sweep", async () => {
    const rows = await db.transaction(
      (tx) => pendingRows(tx, tenant),
      READ_ONLY,
    );
    const threads = new Set(rows.map((r) => r.thread_id));
    for (const thread of threads)
      await isolated(`schedule thread ${thread}`, () =>
        decideThread(pass, thread),
      );
  });
  // A run whose input is durable but that never went (its host died, or the writer was held
  // when it fired) runs on from the log (found by the F10.5 drill).
  // ponytail: reads every schedule thread's log each tick, old ones included; index threads
  // with an open turn if schedules pile up threads.
  const threads = await db.transaction(
    (tx) => scheduleThreads(tx, tenant),
    READ_ONLY,
  );
  // Each look reads the log, then queues the run and returns without waiting for it: awaited,
  // so the run is queued (and counted by idle()) before the tick ends.
  // ponytail: a run in flight here gets a no-op resume queued behind it each tick; track
  // in-flight branches if ticks ever outpace runs.
  await Promise.all(
    threads.map((id) =>
      isolated(`recovering ${id}`, async () => {
        const main = await log.mainBranch(id);
        if (main.ok) await recovery.look(tenant, id, main.value);
      }),
    ),
  );
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
  const last = await db.transaction(
    (tx) => lastOccurrence(tx, tenant, id),
    READ_ONLY,
  );
  const after = last ?? startedAt;
  const due = occurrences(b.cron, b.timezone, after - LOOKBACK_MS, now).filter(
    (at) => at > after,
  );
  if (due.length === 0) return;
  // A due occurrence may open a new thread: its spec artifacts are durable before it is appended.
  const pin = await pass.started(b.hosted);
  await pin.put(pass.ctx.storeFor(tenant));
  await reserveDue(
    db,
    log,
    pin.event,
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
