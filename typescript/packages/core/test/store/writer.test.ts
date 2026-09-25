import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { reduce } from "../../src/reduce";
import { LOCAL_TENANT, type StoreDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { verifyExport } from "../../src/verify";
import {
  fixture,
  ROOT,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

async function rootWithWriter(holder = "holder-a") {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  return { ...f, writer: unwrap(await f.store.acquire(ROOT, holder)) };
}

describe("append", () => {
  test("appends are read back byte for byte and reduce", async () => {
    const { store, writer, clock } = await rootWithWriter();
    unwrap(await writer.append([started, userInput("hi")]));
    unwrap(await writer.append([turnCompleted]));
    const log = unwrap(await store.read(ROOT));
    expect(log.events.map((e) => e.event.seq)).toEqual([1, 2, 3]);
    expect(log.headVerified).toBe(true);
    const state = reduce(log, clock.now);
    expect(state.turns_completed).toBe(1);
    expect(state.epoch).toBe(1);
    expect(state.status).toBe("idle");
  });

  test("an export imports into a fresh store as the same bytes", async () => {
    const { store, writer } = await rootWithWriter();
    unwrap(await writer.append([started, userInput("hi"), turnCompleted]));
    const bytes = unwrap(await store.exportBranch(ROOT));
    const other = await fixture();
    unwrap(await other.store.importLog(bytes));
    expect(unwrap(await other.store.exportBranch(ROOT))).toEqual(bytes);
    expect(unwrap(verifyExport(bytes)).headVerified).toBe(true);
  });

  test("a rule violation is rejected before storage and does not poison", async () => {
    const { store, writer } = await rootWithWriter();
    unwrap(await writer.append([started, userInput("hi")]));
    const second = await writer.append([userInput("again")]);
    expect(second.ok ? "ok" : second.error.code).toBe("invalid_transition");
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(2);
    unwrap(await writer.append([turnCompleted]));
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(3);
  });

  test("a batch is all or nothing", async () => {
    const { store, writer } = await rootWithWriter();
    const batch = await writer.append([
      started,
      userInput("a"),
      userInput("b"),
    ]);
    expect(batch.ok ? "ok" : batch.error.code).toBe("invalid_transition");
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(0);
    unwrap(await writer.append([started]));
  });

  test("an alongside callback that fails appends nothing (idempotency receipts)", async () => {
    const { store, writer } = await rootWithWriter();
    unwrap(await writer.append([started]));
    const refused = await writer.append([userInput("hi")], async () => ({
      ok: false,
      error: { code: "invalid_request", message: "the key was taken" },
    }));
    expect(refused.ok ? "ok" : refused.error.code).toBe("invalid_request");
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(1);
    expect(writer.chain.fold.seq).toBe(1);
    unwrap(await writer.append([userInput("hi")]));
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(2);
  });

  test("a read ends at the head it read, whatever is appended while it reads", async () => {
    const path = join(mkdtempSync(join(tmpdir(), "threads-")), "threads.db");
    const base = openBunSqlite(path);
    let between: (() => Promise<void>) | undefined;
    // Another process appends after this read took the branch row, before it reads the lines.
    const db: StoreDriver = {
      ...base,
      transaction: (fn, options) =>
        base.transaction(
          (tx) =>
            fn({
              ...tx,
              all: async (sql, params) => {
                const append = between;
                if (sql.includes("FROM events") && append !== undefined) {
                  between = undefined;
                  await append();
                }
                return tx.all(sql, params);
              },
            }),
          options,
        ),
    };
    const { store } = await fixture(LOCAL_TENANT, db);
    const other = await fixture(LOCAL_TENANT, openBunSqlite(path));
    unwrap(await store.createBranch(THREAD, ROOT));
    const writer = unwrap(await other.store.acquire(ROOT, "holder-a"));
    unwrap(await writer.append([started, userInput("hi")]));
    between = async () => {
      unwrap(await writer.append([turnCompleted]));
    };
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(2);
    expect(unwrap(await store.read(ROOT)).fold.seq).toBe(3);
  });

  test("a draft that fails its schema is an invalid line", async () => {
    const { writer } = await rootWithWriter();
    const bad = await writer.append([
      // @ts-expect-error: a reason outside the enum, to prove the writer parses its own line
      { ...turnCompleted, data: { reason: "not_a_reason" } },
    ]);
    expect(bad.ok ? "ok" : bad.error.code).toBe("invalid_line");
  });
});

describe("single writer", () => {
  test("a second holder gets branch_busy while the lease is live", async () => {
    const { store } = await rootWithWriter("holder-a");
    const second = await store.acquire(ROOT, "holder-b");
    expect(second.ok ? "ok" : second.error.code).toBe("branch_busy");
  });

  test("a stale writer is rejected with stale_epoch, then poisoned", async () => {
    const { store, writer: stale, clock } = await rootWithWriter("holder-a");
    unwrap(await stale.append([started]));
    clock.now += 31_000; // the lease expired; holder-b takes over
    const fresh = unwrap(await store.acquire(ROOT, "holder-b"));
    expect(fresh.lease.epoch).toBe(2);

    const late = await stale.append([userInput("from the old owner")]);
    expect(late.ok ? "ok" : late.error.code).toBe("stale_epoch");
    const again = await stale.append([userInput("still the old owner")]);
    expect(again.ok ? "ok" : again.error.code).toBe("writer_poisoned");

    unwrap(await fresh.append([userInput("from the new owner")]));
    const log = unwrap(await store.read(ROOT));
    expect(log.events.map((e) => e.event.epoch)).toEqual([1, 2]);
  });

  test("an expired lease rejects its holder even before a takeover", async () => {
    const { writer, clock } = await rootWithWriter();
    clock.now += 31_000;
    const late = await writer.append([started]);
    expect(late.ok ? "ok" : late.error.code).toBe("stale_epoch");
  });

  test("renewing keeps the lease; a taken lease can't be renewed", async () => {
    const { store, writer, clock } = await rootWithWriter("holder-a");
    clock.now += 20_000;
    unwrap(await writer.renew(30_000));
    clock.now += 20_000;
    unwrap(await writer.append([started]));
    clock.now += 31_000;
    unwrap(await store.acquire(ROOT, "holder-b"));
    const renewed = await writer.renew(30_000);
    expect(renewed.ok ? "ok" : renewed.error.code).toBe("stale_epoch");
  });

  test("the new epoch is above every epoch on the chain", async () => {
    const { store, writer, clock } = await rootWithWriter("holder-a");
    unwrap(await writer.append([started]));
    clock.now += 31_000;
    const b = unwrap(await store.acquire(ROOT, "holder-b"));
    unwrap(await b.append([userInput("hi")]));
    clock.now += 31_000;
    expect(unwrap(await store.acquire(ROOT, "holder-a")).lease.epoch).toBe(3);
  });
});
