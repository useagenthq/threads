import { ok } from "../../result";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { reading } from "../../store/driver";
import { Batch, type Mint } from "../../team/batch";
import { readerOf } from "../../team/close";
import { consume } from "../../team/consume";
import { pendingTo, teamRow } from "../../team/rows";

// The team worker's step for the team log (design §4.3, §4.7): mail to the operator (a park
// notice, an operator-started member's task notification, a returned message) is taken as a
// receipt under the team log's writer. A held lease leaves it to its holder.

/** Consumes the team log's pending mail when there is any and its lease is free. */
export async function takeTeamLogMail(
  log: LogStore,
  artifacts: ArtifactStore,
  team: string,
  mint: Mint | undefined,
): Promise<void> {
  const row = await reading(log.driver, (tx) => teamRow(tx, team));
  if (
    row === undefined ||
    (await reading(log.driver, (tx) => pendingTo(tx, team, null))).length === 0
  )
    return;
  const writer = await log.acquire(
    row.team_log_branch_id,
    `team-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  try {
    if (header === undefined) throw new Error("a team log has a header");
    const appended = await w.appendDecided(async (tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, mint);
      await consume({
        tx: tx.tx,
        chain: tx.chain,
        batch,
        threadId: header.thread_id,
        branchId: header.branch_id,
        read: readerOf(artifacts),
      });
      return ok(batch.drafts);
    });
    if ("ok" in appended && !appended.ok)
      throw new Error(`team log ${team}: ${appended.error.message}`);
  } finally {
    await w.release();
  }
}
