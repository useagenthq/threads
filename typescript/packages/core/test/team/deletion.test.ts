import { describe, expect, test } from "bun:test";
import { z } from "zod";
import type { ThreadId } from "../../src/log";
import {
  deleteTenant,
  deleteThread,
  TEAM_TABLES,
} from "../../src/store/deletion";
import { caseStore, loadCase } from "../conformance/cases";
import { ENGINE } from "../store/engine";
import { code, T0, unwrap } from "../store/helpers";
import { caseLogs, exec, query, type Team, teamStore } from "./kit";

// Deleting with teams (Gate 1 §4.15): the deletion set is a fixed point over subagent and
// team_member children and each doomed lead's team log, deleted in one transaction, refused
// busy while anything in it runs and thread_in_team for a member or team log without its lead.

const REBIND = "team-failed-rebind-bounces";
const ALL = ["lead", "researcher", "team", "writer"];
/** A trigger that fails the second tombstone insert, in each engine's dialect. */
const CRASH_ON_SECOND_TOMBSTONE = {
  sqlite: [
    `CREATE TRIGGER crash BEFORE INSERT ON tombstones
      WHEN (SELECT COUNT(*) FROM tombstones) >= 1
      BEGIN SELECT RAISE(ABORT, 'crash'); END`,
  ],
  postgres: [
    `CREATE FUNCTION crash() RETURNS trigger LANGUAGE plpgsql AS $$
      BEGIN
        IF (SELECT COUNT(*) FROM tombstones) >= 1 THEN RAISE EXCEPTION 'crash'; END IF;
        RETURN NEW;
      END $$`,
    "CREATE TRIGGER crash BEFORE INSERT ON tombstones FOR EACH ROW EXECUTE FUNCTION crash()",
  ],
} as const;
const Count = z.array(z.strictObject({ n: z.int() }));

async function count(
  t: Team,
  sql: string,
  params: readonly string[] = [],
): Promise<number> {
  return (
    Count.parse(
      await query(t.db, `SELECT COUNT(*) AS n FROM ${sql}`, params),
    )[0]?.n ?? 0
  );
}

function thread(t: Team, label: string): ThreadId {
  const id = t.logs.get(label)?.segments[0]?.header.thread_id;
  if (id === undefined) throw new Error(`no log ${label}`);
  return id;
}

function branch(t: Team, label: string): string {
  return t.logs.get(label)?.segments[0]?.header.branch_id ?? "";
}

async function teamRows(t: Team): Promise<number> {
  let n = 0;
  for (const table of TEAM_TABLES) n += await count(t, table);
  return n;
}

/** Everything a refusal must leave: every thread, every team row. */
async function untouched(t: Team, threads: number): Promise<void> {
  expect(await count(t, "threads")).toBe(threads);
  expect(await count(t, "tombstones")).toBe(0);
  expect(await teamRows(t)).toBeGreaterThan(0);
}

describe("deleting a team", () => {
  test("the lead takes its members, its team log and every row of its team", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    expect(await teamRows(t)).toBeGreaterThan(0);
    expect(
      unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(4);
    expect(await count(t, "threads")).toBe(0);
    expect(await count(t, "tombstones")).toBe(4);
    expect(await teamRows(t)).toBe(0);
  });

  test("a member in the starting window has no thread: its row and task mail go with the lead", async () => {
    const t = await teamStore(
      caseLogs("team-tree-starting-member-pending", ["lead", "team"]),
    );
    expect(await count(t, "team_members WHERE state = 'starting'")).toBe(1);
    expect(
      unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(2);
    expect(await count(t, "tombstones")).toBe(2);
    expect(await teamRows(t)).toBe(0);
  });

  test("a member alone, or the team log alone, is thread_in_team and writes nothing", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    for (const label of ["researcher", "team"]) {
      const refused = await deleteThread(
        t.db,
        t.store.tenant,
        thread(t, label),
        T0,
      );
      expect(code(refused)).toBe("thread_in_team");
      expect(refused.ok ? "" : refused.error.message).toContain(
        thread(t, "lead"),
      );
    }
    await untouched(t, 4);
  });

  test("a live lease anywhere in the set is busy; an expired one is not", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await exec(
      t.db,
      "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, 'w', 9, ?)",
      [branch(t, "writer"), T0 + 1],
    );
    const lead = thread(t, "lead");
    expect(code(await deleteThread(t.db, t.store.tenant, lead, T0))).toBe(
      "busy",
    );
    await untouched(t, 4);
    expect(unwrap(await deleteThread(t.db, t.store.tenant, lead, T0 + 1))).toBe(
      4,
    );
  });

  test("a crash mid-delete leaves everything", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    for (const sql of CRASH_ON_SECOND_TOMBSTONE[ENGINE]) await exec(t.db, sql);
    await expect(
      deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0),
    ).rejects.toThrow("crash");
    await untouched(t, 4);
  });

  test("another tenant's lead is not_found", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    expect(code(await deleteThread(t.db, "other", thread(t, "lead"), T0))).toBe(
      "not_found",
    );
    await untouched(t, 4);
  });

  test("a whole tenant is one set: every thread, no thread_in_team", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    expect(unwrap(await deleteTenant(t.db, t.store.tenant, T0))).toBe(4);
    expect(await teamRows(t)).toBe(0);
    expect(unwrap(await deleteTenant(t.db, t.store.tenant, T0))).toBe(0);
  });

  test("a whole tenant is busy while any thread of it runs", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await exec(
      t.db,
      "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, 'w', 9, ?)",
      [branch(t, "researcher"), T0 + 1],
    );
    expect(code(await deleteTenant(t.db, t.store.tenant, T0))).toBe("busy");
    await untouched(t, 4);
  });
});

describe("deleting a thread with an effect in doubt", () => {
  test("is busy until the effect settles", async () => {
    const c = loadCase("effect-crash-after-begin-idempotent");
    const { db, store } = await caseStore(c);
    const log = unwrap(await store.importLog(c.log ?? new Uint8Array()));
    const id = log.segments[0]?.header.thread_id;
    if (id === undefined) throw new Error("no header");
    const refused = await deleteThread(db, store.tenant, id, T0);
    expect(code(refused)).toBe("busy");
    expect(refused.ok ? "" : refused.error.message).toContain(
      "effect in doubt",
    );
  });
});
