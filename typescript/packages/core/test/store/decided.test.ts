import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { err, ok, type Result } from "../../src/result";
import type { StoreDriver, Writer } from "../../src/store";
import { LEASE_TTL_MS } from "../../src/store";
import { isRefusal } from "../../src/store/writer";
import type { ChainEvent } from "../../src/verify";
import type { LogError } from "../../src/verify/error";
import {
  fixture,
  ROOT,
  rows,
  started,
  THREAD,
  unwrap,
  userInput,
} from "./helpers";

// appendDecided: one transaction that checks the lease and head, lets the decision read the store
// and build the drafts, admits them and commits. A refusal rolls back and the writer goes on.

async function rootWithWriter() {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder-a"));
  unwrap(await writer.append([started]));
  return { ...f, writer };
}

const Rows = z.array(z.strictObject({ n: z.int() }));
const wakes = async (db: StoreDriver): Promise<number> =>
  Rows.parse(await rows(db, "SELECT COUNT(*) AS n FROM pending_wakes"))[0]?.n ??
  0;

/** A decided append that did not refuse: appended, or failed. */
async function decided(
  writer: Writer,
  decide: Parameters<Writer["appendDecided"]>[0],
): Promise<Result<readonly ChainEvent[], LogError>> {
  const outcome = await writer.appendDecided(decide);
  if (isRefusal(outcome)) throw new Error("refused");
  return outcome;
}

describe("appendDecided", () => {
  test("the decision reads the store in the append's transaction and its drafts are appended", async () => {
    const { store, writer } = await rootWithWriter();
    const appended = unwrap(
      await decided(writer, async (tx) => {
        const [row] = Rows.parse(
          await tx.tx.all(
            "SELECT head_seq AS n FROM branches WHERE branch_id = ?",
            [ROOT],
          ),
        );
        expect(row?.n).toBe(tx.chain.fold.seq);
        return ok([userInput(`after ${row?.n}`)]);
      }),
    );
    expect(appended.map((e) => e.event.seq)).toEqual([2]);
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(2);
  });

  test("a refusal rolls back what the decision wrote, appends nothing, and leaves the writer usable", async () => {
    const { store, writer, db } = await rootWithWriter();
    const refused = await writer.appendDecided(async (tx) => {
      await tx.tx.run(
        "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, 'c')",
        [ROOT],
      );
      return err("mailbox_full" as const);
    });
    expect(refused).toEqual({ kind: "refused", refusal: "mailbox_full" });
    expect(await wakes(db)).toBe(0);
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(1);
    unwrap(await writer.append([userInput("hi")]));
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(2);
  });

  test("drafts that fail admission roll back and don't poison", async () => {
    const { store, writer } = await rootWithWriter();
    const bad = await decided(writer, async () =>
      ok([userInput("a"), userInput("b")]),
    );
    expect(bad.ok ? "ok" : bad.error).toMatchObject({
      code: "invalid_transition",
    });
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(1);
    unwrap(await decided(writer, async () => ok([userInput("hi")])));
  });

  test("a lost lease is stale_epoch before the decision runs, and poisons the writer", async () => {
    const { store, writer, clock } = await rootWithWriter();
    clock.now += LEASE_TTL_MS + 1;
    unwrap(await store.acquire(ROOT, "holder-b"));
    let ran = false;
    const stale = await decided(writer, async () => {
      ran = true;
      return ok([userInput("late")]);
    });
    expect(stale.ok ? "ok" : stale.error).toMatchObject({
      code: "stale_epoch",
    });
    expect(ran).toBe(false);
    const after = await decided(writer, async () => ok([userInput("again")]));
    expect(after.ok ? "ok" : after.error).toMatchObject({
      code: "writer_poisoned",
    });
  });
});

describe("an empty decided batch", () => {
  test("commits nothing and moves nothing", async () => {
    const { store, writer } = await rootWithWriter();
    const before = writer.chain;
    let moved = false;
    void (async () => {
      await writer.moved();
      moved = true;
    })();
    expect(unwrap(await decided(writer, async () => ok([])))).toEqual([]);
    await Promise.resolve();
    expect(moved).toBe(false);
    expect(writer.chain).toBe(before);
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(1);
  });
});
