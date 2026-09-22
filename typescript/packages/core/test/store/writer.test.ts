import { describe, expect, test } from "bun:test";
import { reduce } from "../../src/reduce";
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

function rootWithWriter(holder = "holder-a") {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  return { ...f, writer: unwrap(f.store.acquire(ROOT, holder)) };
}

describe("append", () => {
  test("appends are read back byte for byte and reduce", () => {
    const { store, writer, clock } = rootWithWriter();
    unwrap(writer.append([started, userInput("hi")]));
    unwrap(writer.append([turnCompleted]));
    const log = unwrap(store.read(ROOT));
    expect(log.events.map((e) => e.event.seq)).toEqual([1, 2, 3]);
    expect(log.headVerified).toBe(true);
    const state = reduce(log, clock.now);
    expect(state.turns_completed).toBe(1);
    expect(state.epoch).toBe(1);
    expect(state.status).toBe("idle");
  });

  test("an export imports into a fresh store as the same bytes", () => {
    const { store, writer } = rootWithWriter();
    unwrap(writer.append([started, userInput("hi"), turnCompleted]));
    const bytes = unwrap(store.exportBranch(ROOT));
    const other = fixture();
    unwrap(other.store.importLog(bytes));
    expect(unwrap(other.store.exportBranch(ROOT))).toEqual(bytes);
    expect(unwrap(verifyExport(bytes)).headVerified).toBe(true);
  });

  test("a rule violation is rejected before storage and does not poison", () => {
    const { store, writer } = rootWithWriter();
    unwrap(writer.append([started, userInput("hi")]));
    const second = writer.append([userInput("again")]);
    expect(second.ok ? "ok" : second.error.code).toBe("invalid_transition");
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(2);
    unwrap(writer.append([turnCompleted]));
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(3);
  });

  test("a batch is all or nothing", () => {
    const { store, writer } = rootWithWriter();
    const batch = writer.append([started, userInput("a"), userInput("b")]);
    expect(batch.ok ? "ok" : batch.error.code).toBe("invalid_transition");
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(0);
    unwrap(writer.append([started]));
  });

  test("a draft that fails its schema is an invalid line", () => {
    const { writer } = rootWithWriter();
    const bad = writer.append([
      // @ts-expect-error: a reason outside the enum, to prove the writer parses its own line
      { ...turnCompleted, data: { reason: "not_a_reason" } },
    ]);
    expect(bad.ok ? "ok" : bad.error.code).toBe("invalid_line");
  });
});

describe("single writer", () => {
  test("a second holder gets branch_busy while the lease is live", () => {
    const { store } = rootWithWriter("holder-a");
    const second = store.acquire(ROOT, "holder-b");
    expect(second.ok ? "ok" : second.error.code).toBe("branch_busy");
  });

  test("a stale writer is rejected with stale_epoch, then poisoned", () => {
    const { store, writer: stale, clock } = rootWithWriter("holder-a");
    unwrap(stale.append([started]));
    clock.now += 31_000; // the lease expired; holder-b takes over
    const fresh = unwrap(store.acquire(ROOT, "holder-b"));
    expect(fresh.lease.epoch).toBe(2);

    const late = stale.append([userInput("from the old owner")]);
    expect(late.ok ? "ok" : late.error.code).toBe("stale_epoch");
    const again = stale.append([userInput("still the old owner")]);
    expect(again.ok ? "ok" : again.error.code).toBe("writer_poisoned");

    unwrap(fresh.append([userInput("from the new owner")]));
    const log = unwrap(store.read(ROOT));
    expect(log.events.map((e) => e.event.epoch)).toEqual([1, 2]);
  });

  test("an expired lease rejects its holder even before a takeover", () => {
    const { writer, clock } = rootWithWriter();
    clock.now += 31_000;
    const late = writer.append([started]);
    expect(late.ok ? "ok" : late.error.code).toBe("stale_epoch");
  });

  test("renewing keeps the lease; a taken lease can't be renewed", () => {
    const { store, writer, clock } = rootWithWriter("holder-a");
    clock.now += 20_000;
    unwrap(writer.renew(30_000));
    clock.now += 20_000;
    unwrap(writer.append([started]));
    clock.now += 31_000;
    unwrap(store.acquire(ROOT, "holder-b"));
    const renewed = writer.renew(30_000);
    expect(renewed.ok ? "ok" : renewed.error.code).toBe("stale_epoch");
  });

  test("the new epoch is above every epoch on the chain", () => {
    const { store, writer, clock } = rootWithWriter("holder-a");
    unwrap(writer.append([started]));
    clock.now += 31_000;
    const b = unwrap(store.acquire(ROOT, "holder-b"));
    unwrap(b.append([userInput("hi")]));
    clock.now += 31_000;
    expect(unwrap(store.acquire(ROOT, "holder-a")).lease.epoch).toBe(3);
  });
});
