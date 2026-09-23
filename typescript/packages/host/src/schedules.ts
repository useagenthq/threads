import type { Input } from "@threads/core";
import {
  BranchId,
  type EventDraft,
  Int,
  type LogStore,
  type Principal,
  parseRows,
  type SqliteDriver,
  storeConnection,
  ThreadId,
  uuidv7,
} from "@threads/core/host";
import { z } from "zod";
import type { HostContext, HostedAgent } from "./context";
import { type Cron, occurrences, parseCron } from "./cron";

// Schedules: an occurrence is (schedule_id, scheduled instant UTC), claimed by its
// schedule_occurrences row in the transaction that records it, so two schedulers start one run
// and a restart at the boundary never runs it twice. Occurrences that fell due while the host
// was down are recorded as missed; one due while the schedule's previous run is still going is
// recorded as an overlap. Each schedule runs on one thread of the local tenant.

/** spec/api.json Schedule. */
export type Schedule = {
  readonly id: string;
  /** A key of host({agents}). */
  readonly agent: string;
  readonly cron: string;
  /** IANA name; default UTC. */
  readonly timezone?: string;
  readonly input: Input;
};

type Bound = {
  readonly schedule: Schedule;
  readonly cron: Cron;
  readonly hosted: HostedAgent;
  readonly timezone: string;
};

const TENANT = "local";
/** Enough to see both instances of a fall-back hour, so its second one is never run. */
const LOOKBACK_MS = 3 * 3_600_000;

export function bindSchedules(
  ctx: HostContext,
  schedules: readonly Schedule[],
): readonly Bound[] | string {
  const bound: Bound[] = [];
  for (const schedule of schedules) {
    const cron = parseCron(schedule.cron);
    if (!cron.ok) return `schedule ${schedule.id}: ${cron.error}`;
    const hosted = ctx.agents.get(schedule.agent);
    if (hosted === undefined)
      return `schedule ${schedule.id}: no agent ${schedule.agent}`;
    const timezone = schedule.timezone ?? "UTC";
    try {
      new Intl.DateTimeFormat("en-US", { timeZone: timezone });
    } catch {
      return `schedule ${schedule.id}: unknown time zone ${timezone}`;
    }
    bound.push({ schedule, cron: cron.value, hosted, timezone });
  }
  return bound;
}

const Last = z.strictObject({
  occurrence_at: Int,
  thread_id: ThreadId.nullable(),
});

/** One scheduler pass at `now`; `startedAt` is when this host became ready. */
export async function tick(
  ctx: HostContext,
  bound: readonly Bound[],
  startedAt: number,
  now: number,
): Promise<void> {
  const { db } = await storeConnection(ctx.store);
  const { log } = await ctx.open(TENANT);
  for (const b of bound) {
    const last = lastOccurrence(db, b.schedule.id);
    const after = last?.occurrence_at ?? startedAt;
    const due = occurrences(
      b.cron,
      b.timezone,
      after - LOOKBACK_MS,
      now,
    ).filter((at) => at > after);
    for (const at of due) {
      const threadId = last?.thread_id ?? lastThread(db, b.schedule.id);
      await occurrence(ctx, log, db, b, {
        at,
        missed: at <= startedAt,
        threadId,
      });
    }
  }
}

function lastOccurrence(
  db: SqliteDriver,
  scheduleId: string,
): z.infer<typeof Last> | undefined {
  const rows = parseRows(
    Last,
    db.all(
      `SELECT occurrence_at, thread_id FROM schedule_occurrences WHERE schedule_id = ?
        ORDER BY occurrence_at DESC LIMIT 1`,
      [scheduleId],
    ),
  );
  return rows.ok ? rows.value[0] : undefined;
}

function lastThread(db: SqliteDriver, scheduleId: string): ThreadId | null {
  const rows = parseRows(
    z.strictObject({ thread_id: ThreadId }),
    db.all(
      `SELECT thread_id FROM schedule_occurrences WHERE schedule_id = ? AND thread_id IS NOT NULL
        ORDER BY occurrence_at DESC LIMIT 1`,
      [scheduleId],
    ),
  );
  return rows.ok ? (rows.value[0]?.thread_id ?? null) : null;
}

type Due = {
  readonly at: number;
  readonly missed: boolean;
  readonly threadId: ThreadId | null;
};

async function occurrence(
  ctx: HostContext,
  log: LogStore,
  db: SqliteDriver,
  b: Bound,
  due: Due,
): Promise<void> {
  const threadId = due.threadId ?? ThreadId.parse(uuidv7(log.now()));
  const main = log.mainBranch(threadId);
  const branchId = main.ok ? main.value : BranchId.parse(uuidv7(log.now()));
  if (!main.ok && !log.createBranch(threadId, branchId).ok) return;
  const first: readonly EventDraft[] = main.ok
    ? []
    : [b.hosted.runner.started()];
  const writer = await briefly(log, branchId);
  if (!writer.ok) {
    // A run still holds the branch past a scheduler's short hold: the claim records the overlap.
    claim(
      db,
      b,
      due.at,
      { state: "skipped", reason: "overlap", threadId },
      log.now(),
    );
    return;
  }
  const w = writer.value;
  const skip = due.missed
    ? "missed"
    : w.chain.fold.turnOpen
      ? "overlap"
      : undefined;
  const occurrenceId = `${b.schedule.id}@${new Date(due.at).toISOString()}`;
  const data = {
    schedule_id: b.schedule.id,
    occurrence_id: occurrenceId,
    scheduled_for: due.at,
    timezone: b.timezone,
  };
  let claimed = false;
  const recorded = w.fenced(() => {
    const event: EventDraft =
      skip === undefined
        ? {
            type: "schedule_fired",
            type_version: 1,
            critical: true,
            actor: SCHEDULER(b),
            data,
          }
        : {
            type: "schedule_skipped",
            type_version: 1,
            critical: false,
            actor: SCHEDULER(b),
            data: { ...data, reason: skip },
          };
    const first_ = w.append([...first, event], () => {
      claimed = claim(
        db,
        b,
        due.at,
        skip === undefined
          ? { state: "fired", reason: null, threadId }
          : { state: "skipped", reason: skip, threadId },
        log.now(),
      );
      return claimed
        ? { ok: true, value: undefined }
        : {
            ok: false,
            error: { code: "invalid_request", message: "claimed elsewhere" },
          };
    });
    if (!first_.ok || skip !== undefined) return first_;
    const fired = first_.value.at(-1);
    if (fired?.kind !== "event") throw new Error("schedule_fired is known");
    return w.append([input(b, fired.event.event_id)]);
  });
  w.release();
  if (!recorded.ok || !claimed || skip !== undefined) return;
  void ctx.resume(b.hosted, TENANT, principal(b), {
    id: threadId,
    branch: branchId,
  });
}

const HOLD_TRIES = 10;

/** The lease, waiting out another scheduler's short hold; a run's longer hold stays busy. */
async function briefly(
  log: LogStore,
  branchId: BranchId,
): Promise<ReturnType<LogStore["acquire"]>> {
  let writer = log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  for (let i = 0; i < HOLD_TRIES && !writer.ok; i += 1) {
    await Bun.sleep(20);
    writer = log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  }
  return writer;
}

function principal(b: Bound): Principal {
  return { issuer: "scheduler", tenant: TENANT, subject: b.schedule.id };
}

const SCHEDULER = (b: Bound) => ({
  kind: "scheduler" as const,
  principal: principal(b),
});

function input(b: Bound, cause: string): EventDraft {
  const { input: given } = b.schedule;
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor: SCHEDULER(b),
    data: {
      source: "schedule",
      delivery_event_id: cause,
      ...(typeof given === "string"
        ? { text: given }
        : { content: [...given] }),
    },
  };
}

/** Inserts the occurrence's row; false when another scheduler claimed it first. */
function claim(
  db: SqliteDriver,
  b: Bound,
  at: number,
  outcome: {
    readonly state: "fired" | "skipped";
    readonly reason: "missed" | "overlap" | null;
    readonly threadId: string;
  },
  now: number,
): boolean {
  db.run(
    `INSERT INTO schedule_occurrences (tenant_id, schedule_id, occurrence_at, state, reason,
      thread_id, claimed_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING`,
    [
      TENANT,
      b.schedule.id,
      at,
      outcome.state,
      outcome.reason,
      outcome.threadId,
      now,
    ],
  );
  const rows = parseRows(
    z.strictObject({ n: Int }),
    db.all("SELECT changes() AS n", []),
  );
  return rows.ok && rows.value[0]?.n === 1;
}
