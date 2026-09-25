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
  rows,
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
  test("stores the branch, its events and its first lease, and returns a writer that appends", async () => {
    const { store, db } = await fixture();
    const writer = unwrap(
      await store.openBranch({
        threadId: THREAD,
        branchId: ROOT,
        lease: LEASE,
        drafts: [started, userInput("hi")],
      }),
    );
    if (writer === ALREADY_OPEN) throw new Error("new branch");
    expect(writer.lease.epoch).toBe(1);
    expect(
      Rows.parse(
        await rows(
          db,
          "SELECT branch_id, holder_id, epoch, expires_at FROM leases",
          [],
        ),
      ),
    ).toEqual([
      {
        branch_id: ROOT,
        holder_id: "opener",
        epoch: 1,
        expires_at: T0 + 30_000,
      },
    ]);
    unwrap(await writer.append([turnCompleted]));
    const log = unwrap(await store.read(ROOT));
    expect(log.events.map((e) => [e.event.seq, e.event.epoch])).toEqual([
      [1, 1],
      [2, 1],
      [3, 1],
    ]);
  });

  test("an existing branch is already_open, and nothing is written", async () => {
    const { store } = await fixture();
    const opening = {
      threadId: THREAD,
      branchId: ROOT,
      lease: LEASE,
      drafts: [started],
    };
    unwrap(await store.openBranch(opening));
    expect(
      unwrap(
        await store.openBranch({
          ...opening,
          drafts: [started, userInput("x")],
        }),
      ),
    ).toBe(ALREADY_OPEN);
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(1);
  });

  test("drafts that fail admission leave no thread, branch or lease", async () => {
    const { store, db } = await fixture();
    const opened = await store.openBranch({
      threadId: THREAD,
      branchId: ROOT,
      lease: LEASE,
      drafts: [started, userInput("a"), userInput("b")],
    });
    expect(opened.ok).toBe(false);
    for (const table of ["threads", "branches", "leases", "events"])
      expect(
        Rows.parse(await rows(db, `SELECT 1 AS one FROM ${table}`, [])),
      ).toEqual([]);
  });

  test("inside another transaction it is rolled back with it", async () => {
    const { store, db, clock } = await fixture();
    const outer = await atomically<void>(db, async (tx) => {
      unwrap(
        await openBranch(tx, clock.now, {
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
    expect(
      Rows.parse(await rows(db, "SELECT 1 AS one FROM branches", [])),
    ).toEqual([]);
  });
});

describe("branch.open on a stored thread", () => {
  test("is refused: a new branch opens a new thread", async () => {
    const { store } = await fixture();
    unwrap(await store.createBranch(THREAD, ROOT));
    const opened = await store.openBranch({
      threadId: THREAD,
      branchId: CHILD,
      lease: LEASE,
      drafts: [started],
    });
    expect(opened.ok ? "ok" : opened.error.code).toBe("invalid_transition");
    expect((await store.branchState(CHILD)).ok).toBe(false);
  });
});
