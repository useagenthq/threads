import type { BranchId, MailEnvelope } from "../../log";
import type { LogStore } from "../../store";
import type { StoreDriver } from "../../store/driver";
import { reading } from "../../store/driver";
import { claimMail } from "../../team/claim";
import { TEAM_CONSTANTS } from "../../team/constants";
import { mayResume } from "../../team/consume";
import {
  type MemberRow,
  memberRows,
  ownRows,
  pendingFor,
} from "../../team/rows";
import { closed, deadlineDue, leaseFree } from "./scan";

// What the team worker's pass decides for one member before it launches anything (worker.ts):
// whether that member needs work now, and which of its units. Every answer here is a read; the
// unit it returns is what the worker runs.

/** A unit of work on one member: false when it could do nothing and should back off. */
export type Unit = (signal: AbortSignal) => Promise<boolean>;

/** What the decision reads, and the units it may return. */
export type Work = {
  readonly log: LogStore;
  /** The worker's claim token: a row it claimed is one it will wake this member for. */
  readonly token: string;
  readonly claimTtlMs: number | undefined;
  /** Materialize the starting member. */
  readonly materialize: (row: MemberRow) => Unit;
  /** Run the member's branch until it is idle, parked or ended. */
  readonly run: (row: MemberRow, branch: BranchId) => Unit;
  /** Take the branch's pending mail under its own writer (an ended member's refusals). */
  readonly take: (branch: BranchId) => Unit;
};

/** What a member needs now, if anything. */
export async function workFor(
  w: Work,
  db: StoreDriver,
  row: MemberRow,
  flags: { readonly recovering: boolean; readonly cancelled: boolean },
): Promise<Unit | undefined> {
  // A closed team's members start or resume only to apply the lead's cancel.
  if (
    row.state !== "ended" &&
    (await closed(db, row.team_id)) &&
    !flags.cancelled
  )
    return undefined;
  if (row.state === "starting") return w.materialize(row);
  const branch = row.branch_id;
  if (branch === null) return undefined;
  const pending = await reading(db, async (tx) =>
    pendingFor(tx, await ownRows(tx, row.thread_id)),
  );
  const run = w.run(row, branch);
  // A parked member runs again at a run's start (what it waits on may be answered by now), for
  // mail that may resume it, and once an ask or a wait it parked on is due. Waking on a due
  // deadline or on mail to refuse waits for a free lease: its holder does that work, and a
  // launch that can't acquire would relaunch at once, never yielding.
  if (row.state === "parked") {
    const wake =
      flags.recovering || (await wakesParked(w, db, branch, pending));
    return wake ? run : undefined;
  }
  if (row.state === "ended") return await refusing(w, branch, pending);
  // A turn left open with its lease free is resumed (hostless recovery).
  const stranded =
    row.state === "running" &&
    (flags.recovering || (await leaseFree(w.log, branch)));
  return (await claim(w, db, pending)) || stranded ? run : undefined;
}

/** An ended member's pending mail is refused, once its lease is free. */
async function refusing(
  w: Work,
  branch: BranchId,
  pending: readonly MailEnvelope[],
): Promise<Unit | undefined> {
  return pending.length > 0 && (await leaseFree(w.log, branch))
    ? w.take(branch)
    : undefined;
}

/** Mail that may resume the parked member, or, with its lease free, a due ask or wait. */
async function wakesParked(
  w: Work,
  db: StoreDriver,
  branch: BranchId,
  pending: readonly MailEnvelope[],
): Promise<boolean> {
  return (
    (await claim(w, db, pending.filter(mayResume))) ||
    ((await leaseFree(w.log, branch)) && (await deadlineDue(w.log, branch)))
  );
}

/**
 * mail.claim (design §4.6) on the first row this worker would wake the member for: a live claim
 * of another worker means that worker wakes it. Correctness never depends on it: the lease
 * holder consumes.
 */
async function claim(
  w: Work,
  db: StoreDriver,
  mail: readonly MailEnvelope[],
): Promise<boolean> {
  const first = mail[0];
  if (first === undefined) return false;
  const ttl = w.claimTtlMs ?? TEAM_CONSTANTS.claimTtlMs;
  const got = await claimMail(db, first.mail_id, w.token, w.log.now(), ttl);
  return got === "claimed";
}

/** Every member row of the teams this worker drives, the lead's excluded. */
export const rowsOf = (
  db: StoreDriver,
  team: string,
): Promise<readonly MemberRow[]> => reading(db, (tx) => memberRows(tx, team));
