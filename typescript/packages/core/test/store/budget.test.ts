import { describe, expect, test } from "bun:test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { LogStore, memoryArtifacts } from "../../src/store";
import type { Claim } from "../../src/store/budget";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { fixture, unwrap } from "./helpers";

// The budget ledger (store.sql budget_ledger): a reservation fits every
// covering budget or reserves nothing, and reservers on separate connections to one database
// (two processes of one tree) never overspend a limit together.

const claim = (budgetId: string, max: number, amount: number): Claim => ({
  budgetId,
  limit: "max_cost_nanos",
  max,
  amount,
});

describe("budget ledger", () => {
  test("all claims or none; the refusal names the first that doesn't fit", () => {
    const ledger = fixture().store.budgets;
    expect(ledger.reserve("b:1", [claim("thread:t", 10, 6)])).toBeUndefined();
    expect(
      ledger.reserve("b:2", [
        claim("run:t:i", 100, 6),
        claim("thread:t", 10, 6),
      ]),
    ).toEqual({ claim: claim("thread:t", 10, 6), observed: 12 });
    // Nothing of b:2 was reserved, so the run budget still has room.
    expect(ledger.reserve("b:3", [claim("run:t:i", 6, 6)])).toBeUndefined();
    expect(ledger.reserved("b:")).toEqual(["b:1", "b:3"]);
  });

  test("settling replaces the bound with the disposition; releasing frees it", () => {
    const ledger = fixture().store.budgets;
    ledger.reserve("b:1", [claim("thread:t", 10, 8)]);
    ledger.settle("b:1", [["max_cost_nanos", 3]]);
    expect(ledger.reserved("b:")).toEqual([]);
    expect(ledger.reserve("b:2", [claim("thread:t", 10, 7)])).toBeUndefined();
    ledger.release("b:2");
    expect(ledger.reserve("b:3", [claim("thread:t", 10, 7)])).toBeUndefined();
  });

  test("reservers on two connections never exceed a shared limit", () => {
    const path = join(mkdtempSync(join(tmpdir(), "ledger-")), "log.db");
    const open = () =>
      unwrap(LogStore.open(openBunSqlite(path), Date.now, memoryArtifacts()))
        .budgets;
    const ledgers = [open(), open()];
    let seed = 7;
    const random = (n: number): number => {
      seed = (seed * 1103515245 + 12345) % 2 ** 31;
      return seed % n;
    };
    let granted = 0;
    for (let i = 0; i < 200; i++) {
      const amount = 1 + random(9);
      const ledger = ledgers[random(2)];
      const refused = ledger?.reserve(`b${i % 7}:${i}`, [
        claim("thread:root", 250, amount),
        claim(`thread:child${i % 3}`, 120, amount),
      ]);
      if (refused === undefined) granted += amount;
    }
    expect(granted).toBeLessThanOrEqual(250);
    expect(granted).toBeGreaterThan(200);
  });
});
