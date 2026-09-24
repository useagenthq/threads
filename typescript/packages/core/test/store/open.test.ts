import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { err } from "../../src/result";
import { ALREADY_OPEN, openBranch } from "../../src/store/open";
import { atomically } from "../../src/store/tables";
import { logError } from "../../src/verify/error";
import {
  CHILD,
  fixture,
  ROOT,
  started,
  T0,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

// branch.open: a new branch, its first events and its first lease at epoch 1, in one
// transaction; an existing branch is already_open, and a failure leaves nothing behind.

const LEASE = { holderId: "opener", ttlMs: 30_000 };
const Rows = z.array(z.record(z.string(), z.unknown()));

describe("branch.open", () => {
  test("stores the branch, its events and its first lease, and returns a writer that appends", () => {
    const { store, db } = fixture();
    const writer = unwrap(
      store.openBranch({
        threadId: THREAD,
        branchId: ROOT,
        lease: LEASE,
        drafts: [started, userInput("hi")],
      }),
    );
    if (writer === ALREADY_OPEN) throw new Error("new branch");
    expect(writer.lease.epoch).toBe(1);
    expect(Rows.parse(db.all("SELECT * FROM leases", []))).toEqual([
      {
        branch_id: ROOT,
        holder_id: "opener",
        epoch: 1,
        expires_at: T0 + 30_000,
      },
    ]);
    unwrap(writer.append([turnCompleted]));
    const log = unwrap(store.read(ROOT));
    expect(log.events.map((e) => [e.event.seq, e.event.epoch])).toEqual([
      [1, 1],
      [2, 1],
      [3, 1],
    ]);
  });

  test("an existing branch is already_open, and nothing is written", () => {
    const { store } = fixture();
    const opening = {
      threadId: THREAD,
      branchId: ROOT,
      lease: LEASE,
      drafts: [started],
    };
    unwrap(store.openBranch(opening));
    expect(
      unwrap(
        store.openBranch({ ...opening, drafts: [started, userInput("x")] }),
      ),
    ).toBe(ALREADY_OPEN);
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(1);
  });

  test("drafts that fail admission leave no thread, branch or lease", () => {
    const { store, db } = fixture();
    const opened = store.openBranch({
      threadId: THREAD,
      branchId: ROOT,
      lease: LEASE,
      drafts: [started, userInput("a"), userInput("b")],
    });
    expect(opened.ok).toBe(false);
    for (const table of ["threads", "branches", "leases", "events"])
      expect(Rows.parse(db.all(`SELECT * FROM ${table}`, []))).toEqual([]);
  });

  test("inside another transaction it is rolled back with it", () => {
    const { store, db, clock } = fixture();
    const outer = atomically<void>(db, () => {
      unwrap(
        openBranch(db, clock.now, {
          tenantId: store.tenant,
          threadId: THREAD,
          branchId: CHILD,
          lease: LEASE,
          drafts: [started],
        }),
      );
      return err(logError("invalid_request", "the outer write fails"));
    });
    expect(outer.ok).toBe(false);
    expect(Rows.parse(db.all("SELECT * FROM branches", []))).toEqual([]);
  });
});

describe("branch.open on a stored thread", () => {
  test("is refused: a new branch opens a new thread", () => {
    const { store } = fixture();
    unwrap(store.createBranch(THREAD, ROOT));
    const opened = store.openBranch({
      threadId: THREAD,
      branchId: CHILD,
      lease: LEASE,
      drafts: [started],
    });
    expect(opened.ok ? "ok" : opened.error.code).toBe("invalid_transition");
    expect(store.branchState(CHILD).ok).toBe(false);
  });
});
