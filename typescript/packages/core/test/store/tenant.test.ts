import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import type { StoreApiCode } from "../../src/verify/error";
import {
  CHILD,
  code,
  fixture,
  ROOT,
  snapshot,
  started,
  THREAD,
  turnCompleted,
  unwrap,
  userInput,
} from "./helpers";

describe("a store is bound to one tenant", () => {
  test("another tenant's branch is branch_not_found, never data", () => {
    const db = openBunSqlite(":memory:");
    const acme = fixture("acme", db);
    unwrap(acme.store.createBranch(THREAD, ROOT));
    const writer = unwrap(acme.store.acquire(ROOT, "holder-a"));
    unwrap(writer.append([started, userInput("hi"), turnCompleted]));
    unwrap(writer.append([snapshot(null)]));
    const exported = unwrap(acme.store.exportBranch(ROOT));

    const local = fixture(undefined, db).store;
    expect(code(local.read(ROOT))).toBe("branch_not_found");
    expect(code(local.exportBranch(ROOT))).toBe("branch_not_found");
    expect(code(local.acquire(ROOT, "holder-b"))).toBe("branch_not_found");
    const fork = local.beginFork({
      parent: ROOT,
      atSeq: 4,
      branch: CHILD,
      holderId: "holder-b",
    });
    expect(code(fork)).toBe("branch_not_found");
    expect(code(local.importLog(exported))).toBe("branch_not_found");
    expect(code(acme.store.read(ROOT))).toBe("ok");
  });

  test("an absent branch is branch_not_found", () => {
    const { store } = fixture();
    expect(code(store.read(ROOT))).toBe("branch_not_found");
    expect(code(store.acquire(ROOT, "holder-a"))).toBe("branch_not_found");
  });

  test("branch_not_found is an ApiErrorCode of the spec", () => {
    const path = join(
      import.meta.dir,
      "../../../../../spec/schema/api.schema.json",
    );
    const schema = z
      .object({
        $defs: z.object({
          ApiErrorCode: z.object({ enum: z.array(z.string()) }),
        }),
      })
      .parse(JSON.parse(readFileSync(path, "utf8")));
    const codes: readonly StoreApiCode[] = [
      "branch_not_found",
      "branch_exists",
      "not_found",
    ];
    for (const c of codes) expect(schema.$defs.ApiErrorCode.enum).toContain(c);
  });

  test("importing a thread another tenant owns is branch_exists, owner unnamed", () => {
    const db = openBunSqlite(":memory:");
    unwrap(fixture("acme", db).store.createBranch(THREAD, ROOT));
    const elsewhere = fixture();
    unwrap(elsewhere.store.createBranch(THREAD, CHILD));
    const local = fixture(undefined, db).store;
    const refused = local.importLog(
      unwrap(elsewhere.store.exportBranch(CHILD)),
    );
    expect(code(refused)).toBe("branch_exists");
    expect(refused.ok ? "" : refused.error.message).not.toContain("acme");
    expect(code(local.read(CHILD))).toBe("branch_not_found");
  });
});
