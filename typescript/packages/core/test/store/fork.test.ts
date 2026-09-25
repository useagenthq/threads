import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { SandboxId } from "../../src/log";
import { reduce } from "../../src/reduce";
import type { ForkRequest, LogStore, Writer } from "../../src/store";
import {
  CHILD,
  code,
  count,
  fixture,
  ROOT,
  rows,
  run,
  snapshot,
  started,
  T0,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

/** A root at epoch 1: thread_started, a turn, then a snapshot at seq 4. */
async function parentWithSnapshot(expiresAt: number | null = null) {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
  unwrap(
    await writer.append([
      started,
      userInput("hi"),
      turnCompleted,
      snapshot(expiresAt),
    ]),
  );
  return f;
}

function request(atSeq: number): ForkRequest {
  return { parent: ROOT, atSeq, branch: CHILD, holderId: "holder-c" };
}

/** Both fork steps with no sandbox in between: begin, then the fork event. */
async function fork(store: LogStore, atSeq: number) {
  const writer = await store.beginFork(request(atSeq));
  if (!writer.ok) return writer;
  const done = await store.finishFork(writer.value, {
    sandboxId: SandboxId.parse("sbx_child_01"),
    knowledgePolicy: "pinned",
  });
  return done.ok ? writer : done;
}

describe("fork", () => {
  test("a child references its parent's rows and continues its epochs", async () => {
    const { db, store, clock } = await parentWithSnapshot();
    const parentBefore = unwrap(await store.exportBranch(ROOT));
    const forked = unwrap(await fork(store, 4));
    expect(forked.lease.epoch).toBe(2); // the fork event's epoch
    // The fork hands its lease back, so a run takes the child at once, one epoch on.
    const child = unwrap(await store.acquire(CHILD, "holder-r"));
    unwrap(await child.append([userInput("again")]));

    expect(await count(db, CHILD)).toEqual({ n: 2 }); // fork + user_input, no parent row copied
    expect(await count(db, ROOT)).toEqual({ n: 4 });
    expect(unwrap(await store.exportBranch(ROOT))).toEqual(parentBefore);

    const log = unwrap(await store.read(CHILD));
    expect(log.segments.map((s) => s.header.branch_id)).toEqual([ROOT, CHILD]);
    const state = reduce(log, clock.now);
    expect(state.branch_id).toBe(CHILD);
    expect(state.epoch).toBe(3);
    expect(state.head.seq).toBe(6);
    expect(state.status).toBe("in_turn");
  });

  test("the child's export starts with the parent's bytes and imports cleanly", async () => {
    const { store } = await parentWithSnapshot();
    unwrap(await fork(store, 4));
    const parent = unwrap(await store.exportBranch(ROOT));
    const child = unwrap(await store.exportBranch(CHILD));
    const parentBody = parent.subarray(
      0,
      parent.lastIndexOf(0x0a, parent.length - 2) + 1,
    );
    expect(child.subarray(0, parentBody.length)).toEqual(parentBody);

    const other = await fixture();
    unwrap(await other.store.importLog(child));
    expect(unwrap(await other.store.exportBranch(CHILD))).toEqual(child);
    const imported = await other.store.acquire(ROOT, "holder-x");
    expect(imported.ok ? "ok" : imported.error.code).toBe(
      "branch_not_runnable",
    );
  });

  test("forking off a snapshot boundary fails and leaves nothing", async () => {
    const { db, store } = await parentWithSnapshot();
    const bad = await fork(store, 3);
    expect(bad.ok ? "ok" : bad.error.code).toBe("no_snapshot_boundary");
    expect(await rows(db, "SELECT branch_id FROM branches")).toHaveLength(1);
  });

  test("a forking branch is neither readable as ready nor runnable; a failed one stays so", async () => {
    const { store } = await parentWithSnapshot();
    const writer: Writer = unwrap(await store.beginFork(request(4)));
    expect(code(await store.acquire(CHILD, "holder-x"))).toBe(
      "branch_not_runnable",
    );
    unwrap(await store.failFork(writer));
    expect(code(await store.acquire(CHILD, "holder-x"))).toBe(
      "branch_not_runnable",
    );
  });

  test("an expired snapshot can't be forked", async () => {
    const { store, clock } = await parentWithSnapshot(T0 + 1000);
    clock.now = T0 + 1000;
    const bad = await fork(store, 4);
    expect(bad.ok ? "ok" : bad.error.code).toBe("snapshot_expired");
  });
});

describe("storage is a trust boundary", () => {
  test("a byte changed in storage fails the read and refuses a writable open", async () => {
    const { db, store } = await parentWithSnapshot();
    const [stored] = z
      .array(z.object({ line: z.instanceof(Uint8Array) }))
      .parse(await rows(db, "SELECT line FROM events WHERE seq = 2", []));
    const changed = new TextDecoder().decode(stored?.line).replace("hi", "ho");
    await run(db, "UPDATE events SET line = ? WHERE seq = 2", [
      new TextEncoder().encode(changed),
    ]);
    const read = await store.read(ROOT);
    expect(read.ok ? "ok" : [read.error.code, read.error.seq]).toEqual([
      "prev_hash_mismatch",
      3,
    ]);
    const open = await store.acquire(ROOT, "holder-a");
    expect(open.ok ? "ok" : open.error.code).toBe("log_corrupt");
  });

  test("a row that fails its schema is log_corrupt", async () => {
    const { db, store } = await parentWithSnapshot();
    await run(db, "UPDATE branches SET head_hash = 'nope'", []);
    const read = await store.read(ROOT);
    expect(read.ok ? "ok" : read.error.code).toBe("log_corrupt");
  });

  test("a head checkpoint behind the rows is head_mismatch", async () => {
    const { db, store } = await parentWithSnapshot();
    await run(db, "DELETE FROM events WHERE seq = 4", []);
    const read = await store.read(ROOT);
    expect(read.ok ? "ok" : read.error.code).toBe("head_mismatch");
  });

  test("importing over a branch stored with other lines is refused", async () => {
    const a = await parentWithSnapshot();
    const b = await fixture();
    unwrap(await b.store.createBranch(THREAD, ROOT));
    const imported = await b.store.importLog(
      unwrap(await a.store.exportBranch(ROOT)),
    );
    expect(imported.ok ? "ok" : imported.error.code).toBe("seq_conflict");
  });
});
