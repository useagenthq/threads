import { BranchId } from "../../log";
import { ok } from "../../result";
import { isRefusal } from "../../store/writer";
import { Batch } from "../../team/batch";
import { readerOf } from "../../team/close";
import { consume } from "../../team/consume";
import { type RebindCode, rebindFailed } from "../../team/rebind";
import type { WorkerEnv } from "./worker";

// The team worker's appends on a member branch that run no loop: a failed rebind's end, and an
// ended member refusing the mail that still reaches it. Each takes the member's lease, appends
// once and hands the lease back; a lease held elsewhere means its holder does it.

/** A member whose definition can't be rebound here ends failed, under its own writer. */
export async function endUnbound(
  env: WorkerEnv,
  branch: string,
  holder: string,
  code: RebindCode,
): Promise<void> {
  const writer = await env.log.acquire(BranchId.parse(branch), holder);
  if (!writer.ok) return;
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
}

/** An ended member's writer refuses the mail that still reaches it. */
export async function refuseEnded(
  env: WorkerEnv,
  branch: string,
): Promise<void> {
  const writer = await env.log.acquire(
    BranchId.parse(branch),
    `team-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  if (header === undefined) throw new Error("a writer's chain has a header");
  await w.appendDecided(async (tx) => {
    const batch = new Batch(tx.chain.fold.seq, tx.now, env.mint);
    await consume({
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
}
