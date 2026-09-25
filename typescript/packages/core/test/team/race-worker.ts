import { z } from "zod";
import { BranchId } from "../../src/log";
import { LogStore, memoryArtifacts } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { unwrap } from "../store/helpers";
import { runOn } from "./op-run";

// One side of a two-process race (race.test.ts), as its own process: for each job line on stdin it
// opens the shared store file on its own connection and clock, runs one op vector's op under the
// writer of its branch after a random spin, and answers one line on stdout. SQLite serializes the
// two sides' transactions (BEGIN IMMEDIATE) in either order.

const Job = z.object({
  path: z.string(),
  branch: z.string(),
  now: z.int(),
  spinMs: z.number(),
  op: z.object({
    op: z.string(),
    input: z.record(z.string(), z.json()),
    given: z.object({ world: z.string() }).loose(),
  }),
});

async function run(line: string): Promise<unknown> {
  const job = Job.parse(JSON.parse(line));
  const db = openBunSqlite(job.path);
  try {
    const log = unwrap(
      await LogStore.open(db, () => job.now, memoryArtifacts(), "acme"),
    );
    const writer = unwrap(
      await log.acquire(BranchId.parse(job.branch), "race"),
    );
    const until = performance.now() + job.spinMs;
    while (performance.now() < until) {
      // Spin, not sleep: both sides start their transactions as close together as they can.
    }
    return await runOn(writer, job.op);
  } finally {
    await db.close();
  }
}

for await (const line of console) {
  if (line.trim() === "") continue;
  process.stdout.write(
    `${JSON.stringify({ outcome: (await run(line)) ?? null })}\n`,
  );
}
