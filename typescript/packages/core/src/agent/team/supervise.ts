import { generationKey } from "../../fold/host";
import { ThreadId } from "../../log";
import { ok } from "../../result";
import type { LogStore } from "../../store";
import { reading } from "../../store/driver";
import { uuidv7 } from "../../store/encode";
import { Batch, type Mint } from "../../team/batch";
import { memberRows, type TeamRow, teamRow } from "../../team/rows";
import { type RestartPolicy, supervise } from "../../team/supervise";

// The team worker's supervisor step (spec/schema/README.md, "Teams Phase 2" E): when a host
// member's generation has ended with no decision naming it, the host team log's writer records
// one, and a restart starts the next generation in the same append. Mirrors Python's
// agents/team_supervise.py.
//
// Only a host team has one, and only for a name the host still configures: a member whose
// `members` entry is gone keeps its ended row and waits for an operator.

/** members.<name>'s resolved restart policy, by host member name. */
export type Supervision = ReadonlyMap<string, RestartPolicy>;

/**
 * The agent's pin as the host holds it now, with its artifacts stored: what a restart starts the
 * next generation on. Undefined for a name this host no longer defines, or whose pin fails here.
 */
export type HostPin = (agent: string) => Promise<string | undefined>;

export async function superviseHost(
  log: LogStore,
  team: string,
  policies: Supervision,
  mint: Mint | undefined,
  pin?: HostPin,
): Promise<void> {
  if (policies.size === 0) return;
  const row = await reading(log.driver, (tx) => teamRow(tx, team));
  if (row === undefined || row.kind !== "host") return;
  const undecided = await pending(log, row, policies);
  if (undecided.length === 0) return;
  // Pinned before the writer: a pin stores artifacts, which an append's transaction may not.
  const pins = new Map<string, string | undefined>();
  for (const [name] of undecided) pins.set(name, await pin?.(name));
  const writer = await log.acquire(
    row.team_log_branch_id,
    `super-${crypto.randomUUID()}`,
  );
  if (!writer.ok) return;
  const w = writer.value;
  try {
    const appended = await w.appendDecided(async (tx) => {
      const batch = new Batch(tx.chain.fold.seq, tx.now, mint);
      for (const [name, policy] of undecided)
        await supervise(
          {
            tx: tx.tx,
            fold: tx.chain.fold,
            batch,
            team: row,
            threadId: ThreadId.parse(uuidv7(tx.now)),
            configHash: pins.get(name),
          },
          name,
          policy,
        );
      return ok(batch.drafts);
    });
    // A second host that decided the same end from a fold that had gone stale is refused by rule
    // 51 and has rolled back: one decision stands, which is what exactly once means here.
    if (
      "ok" in appended &&
      !appended.ok &&
      appended.error.code !== "invalid_transition"
    )
      throw new Error(`supervisor on ${team}: ${appended.error.message}`);
  } finally {
    await w.release();
  }
}

/** The configured host members whose current generation has ended with no decision on it. */
async function pending(
  log: LogStore,
  team: TeamRow,
  policies: Supervision,
): Promise<readonly (readonly [string, RestartPolicy])[]> {
  const rows = await reading(log.driver, (tx) => memberRows(tx, team.team_id));
  const ended = rows.filter(
    (r) => r.role === "host_member" && r.state === "ended",
  );
  if (ended.length === 0) return [];
  const read = await log.read(team.team_log_branch_id);
  if (!read.ok) return [];
  const { decided } = read.value.fold.team.host;
  return ended.flatMap((r) => {
    const policy = policies.get(r.name);
    return policy === undefined ||
      decided.has(generationKey(r.name, r.generation))
      ? []
      : [[r.name, policy] as const];
  });
}
