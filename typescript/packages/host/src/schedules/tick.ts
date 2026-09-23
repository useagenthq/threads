import { canonicalize, JsonValue, storeConnection } from "@threads/core/host";
import type { HostContext } from "../context";
import { occurrences } from "../cron";
import type { Bound } from "./bind";
import { decideThread, type Pass } from "./decide";
import {
  lastOccurrence,
  pendingRows,
  reserve,
  scheduleThread,
  scheduleThreads,
} from "./rows";

// A scheduler pass. A schedule keeps one thread; an occurrence is (tenant, schedule id,
// scheduled instant UTC). A due occurrence is first reserved as a pending row, with the agent,
// input and timezone it fires with frozen, and then decided under its thread's writer. Every
// pass first decides the tenant's pending rows, whatever schedules are configured now, so a
// reservation outlives a restart or its schedule's removal.

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
  await sweep(pass);
  for (const b of bound) await reserveDue(pass, b, startedAt, now);
  await sweep(pass);
  // A run whose input is durable but that never went (its host died, or the writer was held
  // when it fired) runs on from the log (found by the F10.5 drill).
  for (const id of scheduleThreads(db, tenant)) {
    const main = log.mainBranch(id);
    // ponytail: a run in flight here gets a no-op resume queued behind it each tick; track
    // in-flight branches if ticks ever outpace runs.
    if (main.ok) void ctx.recover(tenant, { id, branch: main.value });
  }
}

/** Decides every pending row of the tenant, thread by thread in occurrence order. */
async function sweep(pass: Pass): Promise<void> {
  const threads = new Set(
    pendingRows(pass.db, pass.tenant).map((r) => r.thread_id),
  );
  for (const thread of threads) await decideThread(pass, thread);
}

/** Reserves the schedule's occurrences due since its last one (or since ready). */
async function reserveDue(
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
  const threadId = await scheduleThread(db, log, id, () =>
    b.hosted.runner.started(),
  );
  const input = canonicalize(JsonValue.parse(b.schedule.input));
  if (!input.ok) throw new Error("a parsed input is canonical JSON");
  for (const at of due)
    reserve(
      db,
      tenant,
      {
        schedule_id: id,
        occurrence_at: at,
        thread_id: threadId,
        agent: b.hosted.key,
        input_json: input.value,
        timezone: b.timezone,
        missed: at <= startedAt,
      },
      log.now(),
    );
}
