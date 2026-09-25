import { expect } from "bun:test";
import type { KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { EventDraft, Writer } from "../../src/store";
import { ALREADY_OPEN } from "../../src/store/open";
import type { VerifiedLog } from "../../src/verify";
import { type Fixture, unwrap } from "../store/helpers";
import { appendable } from "./kit";

// The write path of a recorded team: its logs appended again, event by event, through real
// writers. The lead's first append opens the team log (so the recorded team_opened is not
// appended again), and every other log opens with branch.open.

const HOLDER = "replay";
// The clock follows each event's own time, which moves back and forth between logs.
const FOREVER = 1e12;

type Queue = {
  readonly log: VerifiedLog;
  readonly teamLog: boolean;
  readonly events: KnownEvent[];
  writer: Writer | undefined;
};

/** The event as the draft that appended it: the writer fills in the rest again. */
export function draftOf(e: KnownEvent): EventDraft {
  const {
    seq: _seq,
    thread_id: _thread,
    branch_id: _branch,
    epoch: _epoch,
    time: _time,
    prev_hash: _prev,
    ...draft
  } = e;
  return draft;
}

/** The team log the lead's first append opened, once it has. */
async function takeTeamLog(fx: Fixture, q: Queue): Promise<Writer | undefined> {
  const header = q.log.segments[0]?.header;
  if (header === undefined) throw new Error("a verified log has a header");
  if (!(await fx.store.branchState(header.branch_id)).ok) return undefined;
  return unwrap(await fx.store.acquire(header.branch_id, HOLDER, FOREVER));
}

/**
 * Appends `e` to its log, opening the log's branch with it when it is the first event. False:
 * a team log the lead's first append has not opened yet.
 */
async function append(fx: Fixture, q: Queue, e: KnownEvent): Promise<boolean> {
  const header = q.log.segments[0]?.header;
  if (header === undefined) throw new Error("a verified log has a header");
  if (q.writer === undefined && q.teamLog) {
    q.writer = await takeTeamLog(fx, q);
    if (q.writer === undefined) return false;
  }
  if (q.writer !== undefined) {
    unwrap(await q.writer.append([draftOf(e)]));
    return true;
  }
  const opened = unwrap(
    await fx.store.openBranch({
      threadId: header.thread_id,
      branchId: header.branch_id,
      lease: { holderId: HOLDER, ttlMs: FOREVER },
      drafts: [draftOf(e)],
    }),
  );
  if (opened === ALREADY_OPEN) throw new Error(`${header.branch_id} is open`);
  q.writer = opened;
  return true;
}

/** Appends what of `q` can go now: each event once the rows it moves exist. */
async function drain(fx: Fixture, q: Queue): Promise<boolean> {
  let moved = false;
  for (
    let e = q.events[0];
    e !== undefined && (await appendable(fx.db, e));
    e = q.events[0]
  ) {
    fx.clock.now = e.time;
    if (!(await append(fx, q, e))) break;
    q.events.shift();
    moved = true;
  }
  return moved;
}

/** Re-appends `logs` into `fx`'s store through writers, in an order their rows allow. */
export async function reappend(
  fx: Fixture,
  logs: readonly VerifiedLog[],
): Promise<void> {
  const queues: Queue[] = logs.map((log) => {
    const events = [...knownEvents(log)];
    const teamLog = events[0]?.type === "team_opened";
    return {
      log,
      teamLog,
      events: teamLog ? events.slice(1) : events,
      writer: undefined,
    };
  });
  for (let moved = true; moved; ) {
    moved = false;
    for (const q of queues) moved = (await drain(fx, q)) || moved;
  }
  expect(queues.map((q) => q.events.length)).toEqual(queues.map(() => 0));
}
