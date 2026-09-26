import { afterAll, expect, test } from "bun:test";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { z } from "zod";
import { openThread, sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { type KnownEvent, ThreadId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { unwrap } from "../store/helpers";
import { query } from "../team/kit";
import { inboxRows, OPERATOR } from "./control-item-kit";

// A tree cancel whose target runs in another process (lane 29F). Both the parent's and the
// child's leases are held there, so neither barrier can be appended: each becomes a durable
// control item, and the holder applies them at its own boundaries. The order the design names
// still holds: the parent's runner records its child's agent_finished{cancelled} before its own
// cancelled, because the child's item is applied first and the parent's end waits for it.

const dir = mkdtempSync(join(tmpdir(), "threads-29f-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

/** The other process, and a reader of its output lines. */
function holder(path: string) {
  const proc = Bun.spawn(
    ["bun", join(import.meta.dir, "cancel-tree-worker.ts"), path],
    { stdin: "pipe", stdout: "pipe", stderr: "inherit" },
  );
  const lines = proc.stdout.pipeThrough(new TextDecoderStream()).getReader();
  let buffered = "";
  const next = async (): Promise<string> => {
    for (;;) {
      const at = buffered.indexOf("\n");
      if (at >= 0) {
        const line = buffered.slice(0, at);
        buffered = buffered.slice(at + 1);
        return line;
      }
      const { value, done } = await lines.read();
      if (done) throw new Error("the holder exited");
      buffered += value;
    }
  };
  return {
    next,
    go: () => {
      proc.stdin.write("go\n");
      proc.stdin.flush();
    },
    kill: () => proc.kill(),
  };
}

type Store = ReturnType<typeof sqlite>;

async function events(
  store: Store,
  thread: ThreadId,
): Promise<readonly KnownEvent[]> {
  const { log } = await openStore(store);
  return knownEvents(
    unwrap(await log.read(unwrap(await log.mainBranch(thread)))),
  );
}

/** The thread whose log spawned the other one. */
async function parentThread(store: Store): Promise<ThreadId> {
  const { db } = await storeConnection(store);
  const rows = z
    .array(z.object({ thread_id: ThreadId }))
    .parse(await query(db, "SELECT DISTINCT thread_id FROM branches", []));
  for (const { thread_id } of rows)
    if (
      (await events(store, thread_id)).some((e) => e.type === "agent_spawned")
    )
      return thread_id;
  throw new Error("no parent thread yet");
}

test("a tree cancel of a holder in another process ends the child before the parent", async () => {
  const path = join(dir, "tree");
  const other = holder(path);
  try {
    expect(await other.next()).toBe("running");
    const store = sqlite(path);
    const parentId = await parentThread(store);
    const thread = unwrap(await openThread(store, parentId));
    const done = await thread.cancel(OPERATOR);
    // Both leases are the other process's, so both barriers are durable items, not appends.
    expect(done.ok && "item_key" in done.value).toBe(true);
    const { db } = await storeConnection(store);
    expect(await inboxRows(db)).toHaveLength(2);
    other.go();
    expect(await other.next()).toBe("done cancelled");

    const parent = await events(store, parentId);
    const kinds = parent.map((e) => e.type);
    const finished = kinds.indexOf("agent_finished");
    expect(finished).toBeGreaterThanOrEqual(0);
    // The order of the design: the child's end is on record before the parent's own cancelled.
    expect(finished).toBeLessThan(kinds.indexOf("cancelled"));
    const end = parent.find((e) => e.type === "agent_finished");
    expect(end?.type === "agent_finished" && end.data.status).toBe("cancelled");

    const spawned = parent.find((e) => e.type === "agent_spawned");
    if (spawned?.type !== "agent_spawned") throw new Error("no child");
    const child = await events(store, spawned.data.child_thread_id);
    const barrier = child.map((e) => e.type).indexOf("cancel_requested");
    expect(barrier).toBeGreaterThanOrEqual(0);
    // Nothing new after the barrier the holder applied.
    const later = child.slice(barrier).map((e) => e.type);
    expect(later).not.toContain("model_request");
    expect(later).toContain("cancelled");
    // Each item was applied exactly once, in the append that recorded its barrier.
    const rows = await inboxRows(db);
    expect(rows).toHaveLength(2);
    for (const row of rows) expect(row.consumed_seq).toBeGreaterThan(0);
  } finally {
    other.kill();
  }
}, 30_000);
