import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { err, ok } from "../../src/result";
import type { SqliteDriver } from "../../src/store";
import { LEASE_TTL_MS } from "../../src/store";
import { fixture, ROOT, started, THREAD, unwrap, userInput } from "./helpers";

// appendDecided: one transaction that checks the lease and head, lets the decision read the store
// and build the drafts, admits them and commits. A refusal rolls back and the writer goes on.

function rootWithWriter() {
  const f = fixture();
  unwrap(f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(f.store.acquire(ROOT, "holder-a"));
  unwrap(writer.append([started]));
  return { ...f, writer };
}

const Rows = z.array(z.strictObject({ n: z.int() }));
const wakes = (db: SqliteDriver): number =>
  Rows.parse(db.all("SELECT COUNT(*) AS n FROM pending_wakes", []))[0]?.n ?? 0;

describe("appendDecided", () => {
  test("the decision reads the store in the append's transaction and its drafts are appended", () => {
    const { store, writer } = rootWithWriter();
    const appended = unwrap(
      writer.appendDecided((tx) => {
        const [row] = Rows.parse(
          tx.db.all("SELECT head_seq AS n FROM branches WHERE branch_id = ?", [
            ROOT,
          ]),
        );
        expect(row?.n).toBe(tx.chain.fold.seq);
        return ok([userInput(`after ${row?.n}`)]);
      }),
    );
    expect(appended.map((e) => e.event.seq)).toEqual([2]);
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(2);
  });

  test("a refusal rolls back what the decision wrote, appends nothing, and leaves the writer usable", () => {
    const { store, writer, db } = rootWithWriter();
    const refused = writer.appendDecided((tx) => {
      tx.db.run(
        "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, 'c')",
        [ROOT],
      );
      return err("mailbox_full" as const);
    });
    expect(refused).toEqual(err({ refused: "mailbox_full" }));
    expect(wakes(db)).toBe(0);
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(1);
    unwrap(writer.append([userInput("hi")]));
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(2);
  });

  test("drafts that fail admission roll back and don't poison", () => {
    const { store, writer } = rootWithWriter();
    const bad = writer.appendDecided(() =>
      ok([userInput("a"), userInput("b")]),
    );
    expect(bad.ok ? "ok" : bad.error).toMatchObject({
      code: "invalid_transition",
    });
    expect(unwrap(store.read(ROOT)).fold.seq).toBe(1);
    unwrap(writer.appendDecided(() => ok([userInput("hi")])));
  });

  test("a lost lease is stale_epoch before the decision runs, and poisons the writer", () => {
    const { store, writer, clock } = rootWithWriter();
    clock.now += LEASE_TTL_MS + 1;
    unwrap(store.acquire(ROOT, "holder-b"));
    let decided = false;
    const stale = writer.appendDecided(() => {
      decided = true;
      return ok([userInput("late")]);
    });
    expect(stale.ok ? "ok" : stale.error).toMatchObject({
      code: "stale_epoch",
    });
    expect(decided).toBe(false);
    const after = writer.appendDecided(() => ok([userInput("again")]));
    expect(after.ok ? "ok" : after.error).toMatchObject({
      code: "writer_poisoned",
    });
  });
});
