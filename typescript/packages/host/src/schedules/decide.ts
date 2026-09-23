import { Input } from "@threads/core";
import {
  type ChainEvent,
  type EventDraft,
  err,
  type LogError,
  type LogStore,
  ok,
  type Principal,
  type Result,
  type SqliteDriver,
  type ThreadId,
  type Writer,
} from "@threads/core/host";
import type { HostContext } from "../context";
import {
  decide,
  markOverlaps,
  type Pending,
  pendingOf,
  type Reason,
} from "./rows";

// Deciding a schedule thread's pending occurrences. Only the log proves an overlap (an open
// turn); a busy writer proves nothing, since outbound, intake or a control may hold it briefly.
// Every decision is appended under the thread's writer, in occurrence order, with the row's
// conditional update in the same transaction.

/** One scheduler pass over a tenant. */
export type Pass = {
  readonly ctx: HostContext;
  readonly db: SqliteDriver;
  readonly log: LogStore;
  readonly tenant: string;
};

/** Decides the thread's pending rows and starts the run of the one it fires, if any. */
export async function decideThread(
  pass: Pass,
  threadId: ThreadId,
): Promise<void> {
  const { ctx, db, log, tenant } = pass;
  const main = log.mainBranch(threadId);
  const read = main.ok ? log.read(main.value) : undefined;
  if (!main.ok || read?.ok !== true) return;
  if (read.value.fold.turnOpen) markOverlaps(db, tenant, threadId);
  const writer = await briefly(log, main.value);
  // Contention is not an overlap: the rows stay pending for the next tick.
  if (!writer.ok) return;
  let fired: Pending | undefined;
  try {
    for (const row of pendingOf(db, tenant, threadId)) {
      const reason = classify(row, writer.value.chain.fold.turnOpen, ctx);
      // Another scheduler decided it first: its state is current, so stop here.
      if (!logOccurrence(pass, writer.value, row, reason)) break;
      if (reason === null) fired = row;
    }
  } finally {
    writer.value.release();
  }
  const hosted = fired === undefined ? undefined : ctx.agents.get(fired.agent);
  if (fired === undefined || hosted === undefined) return;
  void ctx.resume(hosted, tenant, principal(tenant, fired.schedule_id), {
    id: threadId,
    branch: main.value,
  });
}

/** In order: missed, then an open turn, then fire with the frozen agent if it is still served. */
function classify(
  row: Pending,
  turnOpen: boolean,
  ctx: HostContext,
): Reason | null {
  if (row.reason !== null) return row.reason;
  if (turnOpen) return "overlap";
  return ctx.agents.has(row.agent) ? null : "removed";
}

/**
 * Appends the row's event and decides the row in one transaction. False when the row is no
 * longer pending (a stale copy): the append rolls back and nothing is logged.
 */
export function logOccurrence(
  pass: Pass,
  writer: Writer,
  row: Pending,
  reason: Reason | null,
): boolean {
  const actor = {
    kind: "scheduler" as const,
    principal: principal(pass.tenant, row.schedule_id),
  };
  const data = {
    schedule_id: row.schedule_id,
    occurrence_id: `${row.schedule_id}@${new Date(row.occurrence_at).toISOString()}`,
    scheduled_for: row.occurrence_at,
    timezone: row.timezone,
  };
  const settle = (added: readonly ChainEvent[]): Result<void, LogError> => {
    const seq = added.at(0)?.event.seq;
    return seq !== undefined &&
      decide(pass.db, pass.tenant, row, { reason, seq })
      ? ok(undefined)
      : err({ code: "invalid_request", message: "decided elsewhere" });
  };
  const done = writer.fenced(() => {
    if (reason !== null)
      return writer.append(
        [
          {
            type: "schedule_skipped",
            type_version: 1,
            critical: false,
            actor,
            data: { ...data, reason },
          },
        ],
        settle,
      );
    const fired = writer.append(
      [
        {
          type: "schedule_fired",
          type_version: 1,
          critical: true,
          actor,
          data,
        },
      ],
      settle,
    );
    const cause = fired.ok ? fired.value.at(0)?.event.event_id : undefined;
    return cause === undefined
      ? fired
      : writer.append([input(row, actor, cause)]);
  });
  return done.ok;
}

function input(
  row: Pending,
  actor: EventDraft["actor"],
  cause: string,
): EventDraft {
  // The row's frozen input: canonical JSON the reservation wrote, parsed back (storage is a
  // boundary).
  const given = Input.parse(JSON.parse(row.input_json));
  return {
    type: "user_input",
    type_version: 1,
    critical: true,
    actor,
    data: {
      source: "schedule",
      delivery_event_id: cause,
      ...(typeof given === "string"
        ? { text: given }
        : { content: [...given] }),
    },
  };
}

/** A scheduled run's caller: the schedule itself (spec/api.json Schedule.id). */
export function principal(tenant: string, scheduleId: string): Principal {
  return { issuer: "schedule", tenant, subject: scheduleId };
}

const HOLD_TRIES = 10;

/** The writer, waiting out another scheduler's short hold; a run's longer hold stays busy. */
async function briefly(
  log: LogStore,
  branchId: Parameters<LogStore["acquire"]>[0],
): Promise<ReturnType<LogStore["acquire"]>> {
  let writer = log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  for (let i = 0; i < HOLD_TRIES && !writer.ok; i += 1) {
    await Bun.sleep(20);
    writer = log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  }
  return writer;
}
