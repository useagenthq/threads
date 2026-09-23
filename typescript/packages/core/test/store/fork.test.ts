import { describe, expect, test } from "bun:test";
import { SandboxId } from "../../src/log";
import { reduce } from "../../src/reduce";
import type { ForkRequest, LogStore, Writer } from "../../src/store";
import {
  CHILD,
  code,
  count,
  fixture,
  ROOT,
  snapshot,
  started,
  T0,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

/** A root at epoch 1: thread_started, a turn, then a snapshot at seq 4. */
function parentWithSnapshot(expiresAt: number | null = null) {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(
    writer.append([
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
function fork(store: LogStore, atSeq: number) {
  const writer = store.beginFork(request(atSeq));
  if (!writer.ok) return writer;
  const done = store.finishFork(writer.value, {
    sandboxId: SandboxId.parse("sbx_child_01"),
    knowledgePolicy: "pinned",
  });
  return done.ok ? writer : done;
}

describe("fork", () => {
  test("a child references its parent's rows and continues its epochs", () => {
    const { db, store, clock } = parentWithSnapshot();
    const parentBefore = unwrap(store.exportBranch(ROOT));
    const child = unwrap(fork(store, 4));
    unwrap(child.append([userInput("again")]));

    expect(count(db, CHILD)).toEqual({ n: 2 }); // fork + user_input, no parent row copied
    expect(count(db, ROOT)).toEqual({ n: 4 });
    expect(unwrap(store.exportBranch(ROOT))).toEqual(parentBefore);

    const log = unwrap(store.read(CHILD));
    expect(log.segments.map((s) => s.header.branch_id)).toEqual([ROOT, CHILD]);
    const state = reduce(log, clock.now);
    expect(state.branch_id).toBe(CHILD);
    expect(state.epoch).toBe(2);
    expect(state.head.seq).toBe(6);
    expect(state.status).toBe("in_turn");
  });

  test("the child's export starts with the parent's bytes and imports cleanly", () => {
    const { store } = parentWithSnapshot();
    unwrap(fork(store, 4));
    const parent = unwrap(store.exportBranch(ROOT));
    const child = unwrap(store.exportBranch(CHILD));
    const parentBody = parent.subarray(
      0,
      parent.lastIndexOf(0x0a, parent.length - 2) + 1,
    );
    expect(child.subarray(0, parentBody.length)).toEqual(parentBody);

    const other = fixture();
    unwrap(other.store.importLog(child));
    expect(unwrap(other.store.exportBranch(CHILD))).toEqual(child);
    const imported = other.store.acquire(ROOT, "holder-x");
    expect(imported.ok ? "ok" : imported.error.code).toBe(
      "branch_not_runnable",
    );
  });

  test("forking off a snapshot boundary fails and leaves nothing", () => {
    const { db, store } = parentWithSnapshot();
    const bad = fork(store, 3);
    expect(bad.ok ? "ok" : bad.error.code).toBe("no_snapshot_boundary");
    expect(db.all("SELECT branch_id FROM branches", [])).toHaveLength(1);
  });

  test("a forking branch is neither readable as ready nor runnable; a failed one stays so", () => {
    const { store } = parentWithSnapshot();
    const writer: Writer = unwrap(store.beginFork(request(4)));
    expect(code(store.acquire(CHILD, "holder-x"))).toBe("branch_not_runnable");
    unwrap(store.failFork(writer));
    expect(code(store.acquire(CHILD, "holder-x"))).toBe("branch_not_runnable");
  });

  test("an expired snapshot can't be forked", () => {
    const { store, clock } = parentWithSnapshot(T0 + 1000);
    clock.now = T0 + 1000;
    const bad = fork(store, 4);
    expect(bad.ok ? "ok" : bad.error.code).toBe("snapshot_expired");
  });
});

describe("storage is a trust boundary", () => {
  test("a byte changed in storage fails the read and refuses a writable open", () => {
    const { db, store } = parentWithSnapshot();
    db.run(
      "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), 'hi', 'ho') AS BLOB) WHERE seq = 2",
      [],
    );
    const read = store.read(ROOT);
    expect(read.ok ? "ok" : [read.error.code, read.error.seq]).toEqual([
      "prev_hash_mismatch",
      3,
    ]);
    const open = store.acquire(ROOT, "holder-a");
    expect(open.ok ? "ok" : open.error.code).toBe("log_corrupt");
  });

  test("a row that fails its schema is log_corrupt", () => {
    const { db, store } = parentWithSnapshot();
    db.run("UPDATE branches SET head_hash = 'nope'", []);
    const read = store.read(ROOT);
    expect(read.ok ? "ok" : read.error.code).toBe("log_corrupt");
  });

  test("a head checkpoint behind the rows is head_mismatch", () => {
    const { db, store } = parentWithSnapshot();
    db.run("DELETE FROM events WHERE seq = 4", []);
    const read = store.read(ROOT);
    expect(read.ok ? "ok" : read.error.code).toBe("head_mismatch");
  });

  test("importing over a branch stored with other lines is refused", () => {
    const a = parentWithSnapshot();
    const b = fixture();
    unwrap(b.store.createBranch(THREAD, ROOT));
    const imported = b.store.importLog(unwrap(a.store.exportBranch(ROOT)));
    expect(imported.ok ? "ok" : imported.error.code).toBe("seq_conflict");
  });
});
