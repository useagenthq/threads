import { onTeamLog } from "../../src/agent/team/team-log";
import { ThreadId } from "../../src/log";
import { ok } from "../../src/result";
import { LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { uuidv7 } from "../../src/store/encode";
import {
  ensureHostTeam,
  type HostMemberStart,
  type OnTeamLog,
} from "../../src/team/host-open";
import type { HostTeamIds } from "../../src/team/host-team";
import { unwrap } from "../store/helpers";

// The other side of the host team's lazy-open race (host-open-drills.test.ts), as its own process:
// it opens the same tenant's host team on the same store file. Both sides derive the same three ids,
// so one wins the branch's primary key and the other gets already_open. It prints `ready`, waits
// for the go line so the two opens overlap, then prints `opened`.

/** The tenant whose host team both sides of the race open. */
export const TENANT = "acme";
/** billing's pinned definition, as the host team cases of the corpus hash it. */
export const CONFIG =
  "5800e46921bd898ffefe26cbb45e8038fd946b9719bd1f0e155c1d94aa9f459b";

/** One configured host member, its root thread minted by this process (as the host mints it). */
export function billingStart(now: number): HostMemberStart {
  return {
    agent: "billing",
    configHash: CONFIG,
    threadId: ThreadId.parse(uuidv7(now)),
  };
}

/** How `ensureHostTeam` appends to an already-open host team log, as the host wires it. */
export function appendTo(log: LogStore): OnTeamLog {
  return async (ids: HostTeamIds, decide) => {
    await onTeamLog(log, ids.branchId, async (tx, batch) => {
      for (const draft of await decide(tx.tx)) batch.add(draft);
    });
    return ok(undefined);
  };
}

/** The tenant's host team, opened (or already open) through `log`. */
export function openOn(
  log: LogStore,
  holder: string,
): ReturnType<typeof ensureHostTeam> {
  const now = log.now();
  return ensureHostTeam(
    log,
    TENANT,
    [billingStart(now)],
    now,
    holder,
    appendTo(log),
  );
}

async function race(path: string, go: string): Promise<void> {
  const db = openBunSqlite(path);
  const log = unwrap(
    await LogStore.open(db, Date.now, memoryArtifacts(), TENANT),
  );
  process.stdout.write("ready\n");
  // Both sides wait for the same line, so their opens overlap.
  for await (const line of console) {
    if (line.trim() !== go) continue;
    const opened = await openOn(log, "other");
    process.stdout.write(opened.ok ? "opened\n" : "failed\n");
    return;
  }
}

if (import.meta.main) {
  const [path, go] = process.argv.slice(2);
  if (path === undefined || go === undefined)
    throw new Error("host-open-worker.ts <path> <go>");
  await race(path, go);
}
