import { BranchId } from "../../log";
import { ok } from "../../result";
import { isRefusal } from "../../store/writer";
import { Batch } from "../../team/batch";
import { readerOf } from "../../team/close";
import { type Consumed, consume } from "../../team/consume";
import { type RebindCode, rebindFailed } from "../../team/rebind";
import type { WorkerEnv } from "./worker";

// The appends on a team branch that run no loop: a failed rebind's end, and the consume of the
// mail waiting for a branch nothing is running (an ended member's refusals, an idle lead's
// receipts). Each takes the branch's lease, appends once and hands the lease back; a lease held
// elsewhere means its holder does it.

/**
 * A member whose definition can't be rebound here ends failed, under its own writer. False: its
 * lease is held elsewhere, and its holder does it.
 */
export async function endUnbound(
  env: WorkerEnv,
  branch: string,
  holder: string,
  code: RebindCode,
): Promise<boolean> {
  const writer = await env.log.acquire(BranchId.parse(branch), holder);
  if (!writer.ok) return false;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  if (header === undefined) throw new Error("a writer's chain has a header");
  const ended = await w.appendDecided(async (tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, env.mint);
    await rebindFailed(
      {
        tx: tx.tx,
        chain: tx.chain,
        batch,
        threadId: header.thread_id,
        branchId: branch,
      },
      code,
      tx.now,
    );
    return ok(batch.drafts);
  });
  await w.release();
  if (isRefusal(ended)) throw new Error("a failed rebind never refuses");
  if (!ended.ok) throw new Error(`member end: ${ended.error.message}`);
  return true;
}

/** What taking a branch's pending mail needs of the worker's environment. */
export type MailEnv = Pick<WorkerEnv, "log" | "artifacts" | "mint">;

/**
 * The branch's own writer takes its pending mail (design §4.7): an ended member's refusals, an
 * idle member's or an idle lead's receipts, which open its turn. Undefined: its lease is held
 * elsewhere, and its holder consumes.
 */
export async function takeMail(
  env: MailEnv,
  branch: string,
): Promise<Consumed | undefined> {
  const writer = await env.log.acquire(
    BranchId.parse(branch),
    `team-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return undefined;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  if (header === undefined) throw new Error("a writer's chain has a header");
  let taken: Consumed = { status: "nothing_pending" };
  const appended = await w.appendDecided(async (tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, env.mint);
    taken = await consume({
      tx: tx.tx,
      chain: tx.chain,
      batch,
      threadId: header.thread_id,
      branchId: branch,
      read: readerOf(env.artifacts),
    });
    return ok(batch.drafts);
  });
  await w.release();
  if (!isRefusal(appended) && !appended.ok)
    throw new Error(`consume on ${branch}: ${appended.error.message}`);
  return taken;
}
