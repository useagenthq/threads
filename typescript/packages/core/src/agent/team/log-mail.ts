import { ok } from "../../result";
import type { LogStore } from "../../store";
import type { ArtifactStore } from "../../store/artifacts";
import { Batch, type Mint } from "../../team/batch";
import { readerOf } from "../../team/close";
import { consume } from "../../team/consume";
import { pendingTo, teamRow } from "../../team/rows";

// The team worker's step for the team log (design §4.3, §4.7): mail to the operator (a park
// notice, an operator-started member's task notification, a returned message) is taken as a
// receipt under the team log's writer. A held lease leaves it to its holder.

/** Consumes the team log's pending mail when there is any and its lease is free. */
export function takeTeamLogMail(
  log: LogStore,
  artifacts: ArtifactStore,
  team: string,
  mint: Mint | undefined,
): void {
  const row = teamRow(log.driver, team);
  if (row === undefined || pendingTo(log.driver, team, null).length === 0)
    return;
  const writer = log.acquire(
    row.team_log_branch_id,
    `team-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return;
  const w = writer.value;
  const header = w.chain.segments[0]?.header;
  try {
    if (header === undefined) throw new Error("a team log has a header");
    const appended = w.appendDecided((tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, mint);
      consume({
        db: tx.db,
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
    w.release();
  }
}
