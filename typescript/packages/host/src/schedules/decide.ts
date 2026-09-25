import {
  type ChainEvent,
  type EventDraft,
  err,
  knownEvents,
  type LogError,
  type LogStore,
  ok,
  type Principal,
  READ_ONLY,
  type Result,
  type ThreadId,
  type Tx,
  uuidv7,
  type Writer,
} from "@threads/core/host";
import { pinMatches } from "../context";
import type { Pass } from "./pass";
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

/** Decides the thread's pending rows and starts the run of the one it fires, if any. */
export async function decideThread(
  pass: Pass,
  threadId: ThreadId,
): Promise<void> {
  const { ctx, db, log, tenant } = pass;
  const main = await log.mainBranch(threadId);
  const read = main.ok ? await log.read(main.value) : undefined;
  if (!main.ok || read?.ok !== true) return;
  if (read.value.fold.turnOpen)
    await db.transaction((tx) => markOverlaps(tx, tenant, threadId));
  const pending = await db.transaction(
    (tx) => pendingOf(tx, tenant, threadId),
    READ_ONLY,
  );
  const pins = await pinsOf(pass, pending);
  const writer = await briefly(log, main.value);
  // Contention is not an overlap: the rows stay pending for the next tick.
  if (!writer.ok) return;
  let fired: Pending | undefined;
  try {
    fired = await decideRows(pass, writer.value, threadId, pins);
  } finally {
    await writer.value.release();
  }
  const hosted = fired === undefined ? undefined : ctx.agents.get(fired.agent);
  if (fired === undefined || hosted === undefined) return;
  void ctx.resume(hosted, tenant, principal(tenant, fired.schedule_id), {
    id: threadId,
    branch: main.value,
  });
}

/** Decides the pending rows in order under the writer; the one it fires, if any. */
async function decideRows(
  pass: Pass,
  writer: Writer,
  threadId: ThreadId,
  pins: ReadonlyMap<string, EventDraft>,
): Promise<Pending | undefined> {
  const events = knownEvents(writer.chain);
  let fired: Pending | undefined;
  const pending = await pass.db.transaction(
    (tx) => pendingOf(tx, pass.tenant, threadId),
    READ_ONLY,
  );
  for (const row of pending) {
    const started = pins.get(row.agent);
    // Reserved by another scheduler after the pins were taken: it waits for the next tick.
    // Only an agent this host no longer serves is removed.
    if (started === undefined && pass.ctx.agents.has(row.agent)) break;
    const runs = started !== undefined && pinMatches(events, started);
    const reason = classify(row, writer.chain.fold.turnOpen, runs);
    // Another scheduler decided it first: its state is current, so stop here.
    if (!(await logOccurrence(pass, writer, row, reason))) break;
    if (reason === null) fired = row;
  }
  return fired;
}

/**
 * In order: missed, then an open turn, then fire with the frozen agent, if it is still served
 * with the config the thread was started with (a thread's pin never changes).
 */
function classify(
  row: Pending,
  turnOpen: boolean,
  runs: boolean,
): Reason | null {
  if (row.reason !== null) return row.reason;
  if (turnOpen) return "overlap";
  return runs ? null : "removed";
}

/** The thread_started each served agent of the rows would pin now, by agent key. */
async function pinsOf(
  pass: Pass,
  rows: readonly Pending[],
): Promise<ReadonlyMap<string, EventDraft>> {
  const pins = new Map<string, EventDraft>();
  for (const key of new Set(rows.map((r) => r.agent))) {
    const hosted = pass.ctx.agents.get(key);
    if (hosted !== undefined) pins.set(key, (await pass.started(hosted)).event);
  }
  return pins;
}

/**
 * Appends the row's event (a fired one with its run's user_input) and decides the row in one
 * transaction. False when the row is no longer pending (a stale copy): the append rolls back and
 * nothing is logged. The row's update is a CAS on `state = 'pending'`, so doing it again after
 * an unknown commit decides nothing twice.
 */
export async function logOccurrence(
  pass: Pass,
  writer: Writer,
  row: Pending,
  reason: Reason | null,
): Promise<boolean> {
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
  const settle = async (
    added: readonly ChainEvent[],
    tx: Tx,
  ): Promise<Result<void, LogError>> => {
    const seq = added.at(0)?.event.seq;
    return seq !== undefined &&
      (await decide(tx, pass.tenant, row, { reason, seq }))
      ? ok(undefined)
      : err({ code: "invalid_request", message: "decided elsewhere" });
  };
  if (reason !== null) {
    const skipped = await writer.append(
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
    return skipped.ok;
  }
  // The fired event names its own id, so the run's input in the same append can cite it.
  const cause = uuidv7(pass.log.now());
  const fired = await writer.append(
    [
      {
        type: "schedule_fired",
        type_version: 1,
        critical: true,
        actor,
        data,
        event_id: cause,
      },
      input(row, actor, cause),
    ],
    settle,
  );
  return fired.ok;
}

function input(
  row: Pending,
  actor: EventDraft["actor"],
  cause: string,
): EventDraft {
  const given = row.input;
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
): ReturnType<LogStore["acquire"]> {
  let writer = await log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  for (let i = 0; i < HOLD_TRIES && !writer.ok; i += 1) {
    await Bun.sleep(20);
    writer = await log.acquire(branchId, `schedule-${crypto.randomUUID()}`);
  }
  return writer;
}
