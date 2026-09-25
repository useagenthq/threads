import { ok } from "../../result";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading } from "../../store/driver";
import { Batch, type Mint } from "../../team/batch";
import { readerOf } from "../../team/close";
import { consume } from "../../team/consume";
import { deadline, dueIds } from "../../team/deadline";
import { dueAsks, pendingTo, teamRow } from "../../team/rows";
import { deadlineDue } from "./scan";

// The team worker's step for the team log (design §4.3, §4.7, §4.8, §4.12): mail to the operator
// (a park notice, an operator-started member's task notification, a reply, a returned message)
// is taken as a receipt, then every operator ask or wait whose deadline has passed is closed, all
// under the team log's writer. Team close is a trigger of its own: once the team has closed, the
// step closes every open ask cancelled. A held lease leaves it to its holder.

/** Takes the team log's pending mail and runs its deadline step when either has work. */
export async function takeTeamLogMail(
  log: LogStore,
  artifacts: ArtifactStore,
  team: string,
  mint: Mint | undefined,
): Promise<void> {
  const row = await reading(log.driver, (tx) => teamRow(tx, team));
  if (row === undefined) return;
  const branch = row.team_log_branch_id;
  const closed = row.closed_at !== null;
  const [pending, open] = await reading(log.driver, async (tx) => [
    await pendingTo(tx, team, null),
    await dueAsks(tx, branch, Number.MAX_SAFE_INTEGER),
  ]);
  const work =
    pending.length > 0 ||
    (closed && open.length > 0) ||
    (await deadlineDue(log, branch));
  if (!work) return;
  const writer = await log.acquire(branch, `team-${crypto.randomUUID()}`);
  if (!writer.ok) return;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  try {
    if (header === undefined) throw new Error("a team log has a header");
    const appended = await w.appendDecided(async (tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, mint);
      const ctx = {
        tx: tx.tx,
        chain: tx.chain,
        batch,
        threadId: header.thread_id,
        branchId: header.branch_id,
        read: readerOf(artifacts),
      };
      await consume(ctx);
      const by = closed ? Number.MAX_SAFE_INTEGER : batch.now;
      for (const id of await dueIds(ctx, by)) await deadline(ctx, id);
      return ok(batch.drafts);
    });
    if ("ok" in appended && !appended.ok)
      throw new Error(`team log ${team}: ${appended.error.message}`);
  } finally {
    await w.release();
  }
}
