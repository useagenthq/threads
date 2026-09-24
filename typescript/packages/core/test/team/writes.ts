import { expect } from "bun:test";
import { KnownEvent } from "../../src/log";
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

/**
 * A team log's thread id is minted when the lead's first append opens it (only its branch id is
 * logged), so the replay renames each recorded team log thread to the one it minted.
 */
export type Renamed = Map<string, string>;

/** `value` with every renamed id replaced, as plain JSON. */
export function renamed(value: unknown, ids: Renamed): unknown {
  let text = JSON.stringify(value);
  for (const [from, to] of ids) text = text.replaceAll(from, to);
  return JSON.parse(text);
}

/** The event as the draft that appended it: the writer fills in the rest again. */
export function draftOf(e: KnownEvent, ids: Renamed = new Map()): EventDraft {
  const {
    seq: _seq,
    thread_id: _thread,
    branch_id: _branch,
    epoch: _epoch,
    time: _time,
    prev_hash: _prev,
    ...draft
  } = KnownEvent.parse(renamed(e, ids));
  return draft;
}

/** Takes the team log the lead's first append opened, and learns its minted thread id. */
function takeTeamLog(fx: Fixture, q: Queue, ids: Renamed): Writer | undefined {
  const header = q.log.segments[0]?.header;
  if (header === undefined) throw new Error("a verified log has a header");
  if (!fx.store.branchState(header.branch_id).ok) return undefined;
  const opened = unwrap(fx.store.read(header.branch_id)).segments[0]?.header;
  if (opened === undefined) throw new Error("a stored log has a header");
  ids.set(header.thread_id, opened.thread_id);
  return unwrap(fx.store.acquire(header.branch_id, HOLDER, FOREVER));
}

/**
 * Appends `e` to its log, opening the log's branch with it when it is the first event. False:
 * a team log the lead's first append has not opened yet.
 */
function append(fx: Fixture, q: Queue, e: KnownEvent, ids: Renamed): boolean {
  const header = q.log.segments[0]?.header;
  if (header === undefined) throw new Error("a verified log has a header");
  if (q.writer === undefined && q.teamLog) {
    q.writer = takeTeamLog(fx, q, ids);
    if (q.writer === undefined) return false;
  }
  if (q.writer !== undefined) {
    unwrap(q.writer.append([draftOf(e, ids)]));
    return true;
  }
  const opened = unwrap(
    fx.store.openBranch({
      threadId: header.thread_id,
      branchId: header.branch_id,
      lease: { holderId: HOLDER, ttlMs: FOREVER },
      drafts: [draftOf(e, ids)],
    }),
  );
  if (opened === ALREADY_OPEN) throw new Error(`${header.branch_id} is open`);
  q.writer = opened;
  return true;
}

/** Appends what of `q` can go now: each event once the rows it moves exist. */
function drain(fx: Fixture, q: Queue, ids: Renamed): boolean {
  let moved = false;
  for (
    let e = q.events[0];
    e !== undefined && appendable(fx.db, KnownEvent.parse(renamed(e, ids)));
    e = q.events[0]
  ) {
    fx.clock.now = e.time;
    if (!append(fx, q, e, ids)) break;
    q.events.shift();
    moved = true;
  }
  return moved;
}

/**
 * Re-appends `logs` into `fx`'s store through writers, in an order their rows allow. Returns
 * each recorded team log thread's minted id.
 */
export function reappend(fx: Fixture, logs: readonly VerifiedLog[]): Renamed {
  const ids: Renamed = new Map();
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
    for (const q of queues) moved = drain(fx, q, ids) || moved;
  }
  expect(queues.map((q) => q.events.length)).toEqual(queues.map(() => 0));
  return ids;
}
