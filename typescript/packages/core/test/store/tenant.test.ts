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
  test("another tenant's branch is branch_not_found, never data", async () => {
    const db = openBunSqlite(":memory:");
    const acme = await fixture("acme", db);
    unwrap(await acme.store.createBranch(THREAD, ROOT));
    const writer = unwrap(await acme.store.acquire(ROOT, "holder-a"));
    unwrap(await writer.append([started, userInput("hi"), turnCompleted]));
    unwrap(await writer.append([snapshot(null)]));
    const exported = unwrap(await acme.store.exportBranch(ROOT));

    const local = (await fixture(undefined, db)).store;
    expect(code(await local.read(ROOT))).toBe("branch_not_found");
    expect(code(await local.exportBranch(ROOT))).toBe("branch_not_found");
    expect(code(await local.acquire(ROOT, "holder-b"))).toBe(
      "branch_not_found",
    );
    const fork = local.beginFork({
      parent: ROOT,
      atSeq: 4,
      branch: CHILD,
      holderId: "holder-b",
    });
    expect(code(await fork)).toBe("branch_not_found");
    expect(code(await local.importLog(exported))).toBe("branch_not_found");
    expect(code(await acme.store.read(ROOT))).toBe("ok");
  });

  test("an absent branch is branch_not_found", async () => {
    const { store } = await fixture();
    expect(code(await store.read(ROOT))).toBe("branch_not_found");
    expect(code(await store.acquire(ROOT, "holder-a"))).toBe(
      "branch_not_found",
    );
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
      "invalid_request",
      "sandbox_required",
      "busy",
      "thread_in_team",
      "path_exists",
      "bundle_incomplete",
      "schedule_conflict",
      "io_error",
    ];
    for (const c of codes) expect(schema.$defs.ApiErrorCode.enum).toContain(c);
  });

  test("importing a thread another tenant owns is branch_exists, owner unnamed", async () => {
    const db = openBunSqlite(":memory:");
    unwrap(await (await fixture("acme", db)).store.createBranch(THREAD, ROOT));
    const elsewhere = await fixture();
    unwrap(await elsewhere.store.createBranch(THREAD, CHILD));
    const local = (await fixture(undefined, db)).store;
    const refused = await local.importLog(
      unwrap(await elsewhere.store.exportBranch(CHILD)),
    );
    expect(code(await refused)).toBe("branch_exists");
    expect(refused.ok ? "" : refused.error.message).not.toContain("acme");
    expect(code(await local.read(CHILD))).toBe("branch_not_found");
  });
});
