import { describe, expect, test } from "bun:test";
import { storeOf } from "../../src/agent/sqlite";
import { Feed } from "../../src/store/feed";
import {
  fixture,
  ROOT,
  rows,
  run,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

// The internal feed a telemetry exporter reads the store through: it never appends, and its
// cursors never move back.

async function setup() {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder"));
  unwrap(await writer.append([started, userInput("hi"), turnCompleted]));
  await writer.release();
  const store = storeOf(
    { log: f.store, artifacts: f.artifacts },
    { db: f.db, now: () => 0 },
  );
  return { f, feed: await Feed.open(store, "otel") };
}

describe("Feed", () => {
  test("reads and checkpoints without appending: the head never moves", async () => {
    const { f, feed } = await setup();
    const head = await rows(f.db, "SELECT head_seq, head_hash FROM branches");
    expect(
      unwrap(await feed.changed()).map((b) => [
        b.branch_id,
        b.head_seq,
        b.cursor,
      ]),
    ).toEqual([[ROOT, 3, 0]]);
    expect(unwrap(await feed.chain(ROOT)).events).toHaveLength(3);
    await feed.register();
    await feed.checkpoint([{ branch_id: ROOT, seq: 3 }]);
    expect(
      await rows(f.db, "SELECT head_seq, head_hash FROM branches"),
    ).toEqual(head);
    expect(unwrap(await feed.changed())).toEqual([]);
  });

  test("a checkpoint never moves a cursor back", async () => {
    const { f, feed } = await setup();
    await feed.checkpoint([{ branch_id: ROOT, seq: 3 }]);
    await feed.checkpoint([{ branch_id: ROOT, seq: 1 }]);
    expect(await rows(f.db, "SELECT seq FROM observer_cursors")).toEqual([
      { seq: 3 },
    ]);
  });

  test("a stored line that doesn't verify reads as log_corrupt", async () => {
    const { f, feed } = await setup();
    await run(f.db, "UPDATE events SET line = ? WHERE seq = 2", [
      new TextEncoder().encode("{}"),
    ]);
    const read = await feed.chain(ROOT);
    expect(read.ok ? "ok" : read.error.code).toBe("log_corrupt");
  });
});
