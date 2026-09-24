import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { deleteTenant, deleteThread } from "../../src/store/deletion";
import { caseStore, loadCase } from "../conformance/cases";
import { code, T0, unwrap } from "../store/helpers";
import { liftTeamRefusal, stagedLogs, type Team, teamStore } from "./kit";

// Deleting with teams (Gate 1 §4.15): the deletion set is a fixed point over subagent and
// team_member children and each doomed lead's team log, deleted in one transaction, refused
// busy while anything in it runs and thread_in_team for a member or team log without its lead.

liftTeamRefusal();

const REBIND = "team-failed-rebind-bounces";
const ALL = ["lead", "researcher", "team", "writer"];
const TEAM_TABLES = [
  "teams",
  "team_members",
  "mail",
  "asks",
  "monitors",
  "operator_receipts",
  "team_feed",
];
const Count = z.array(z.strictObject({ n: z.int() }));

function count(t: Team, sql: string, params: readonly string[] = []): number {
  return (
    Count.parse(t.db.all(`SELECT COUNT(*) AS n FROM ${sql}`, params))[0]?.n ?? 0
  );
}

function thread(t: Team, label: string): string {
  const id = t.logs.get(label)?.segments[0]?.header.thread_id;
  if (id === undefined) throw new Error(`no log ${label}`);
  return id;
}

function branch(t: Team, label: string): string {
  return t.logs.get(label)?.segments[0]?.header.branch_id ?? "";
}

const teamRows = (t: Team): number =>
  TEAM_TABLES.reduce((n, table) => n + count(t, table), 0);

/** Everything a refusal must leave: every thread, every team row. */
function untouched(t: Team, threads: number): void {
  expect(count(t, "threads")).toBe(threads);
  expect(count(t, "tombstones")).toBe(0);
  expect(teamRows(t)).toBeGreaterThan(0);
}

describe("deleting a team", () => {
  test("the lead takes its members, its team log and every row of its team", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    expect(teamRows(t)).toBeGreaterThan(0);
    expect(
      unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(4);
    expect(count(t, "threads")).toBe(0);
    expect(count(t, "tombstones")).toBe(4);
    expect(teamRows(t)).toBe(0);
  });

  test("a member in the starting window has no thread: its row and task mail go with the lead", () => {
    const t = teamStore(
      stagedLogs("team-tree-starting-member-pending", ["lead", "team"]),
    );
    expect(count(t, "team_members WHERE state = 'starting'")).toBe(1);
    expect(
      unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(2);
    expect(count(t, "tombstones")).toBe(2);
    expect(teamRows(t)).toBe(0);
  });

  test("a member alone, or the team log alone, is thread_in_team and writes nothing", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    for (const label of ["researcher", "team"]) {
      const refused = deleteThread(t.db, t.store.tenant, thread(t, label), T0);
      expect(code(refused)).toBe("thread_in_team");
      expect(refused.ok ? "" : refused.error.message).toContain(
        thread(t, "lead"),
      );
    }
    untouched(t, 4);
  });

  test("a live lease anywhere in the set is busy; an expired one is not", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    t.db.run(
      "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, 'w', 9, ?)",
      [branch(t, "writer"), T0 + 1],
    );
    const lead = thread(t, "lead");
    expect(code(deleteThread(t.db, t.store.tenant, lead, T0))).toBe("busy");
    untouched(t, 4);
    expect(unwrap(deleteThread(t.db, t.store.tenant, lead, T0 + 1))).toBe(4);
  });

  test("a crash mid-delete leaves everything", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    t.db.exec(`CREATE TRIGGER crash BEFORE INSERT ON tombstones
      WHEN (SELECT COUNT(*) FROM tombstones) >= 1
      BEGIN SELECT RAISE(ABORT, 'crash'); END`);
    expect(() =>
      deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0),
    ).toThrow("crash");
    untouched(t, 4);
  });

  test("another tenant's lead is not_found", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    expect(code(deleteThread(t.db, "acme", thread(t, "lead"), T0))).toBe(
      "not_found",
    );
    untouched(t, 4);
  });

  test("a whole tenant is one set: every thread, no thread_in_team", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    expect(unwrap(deleteTenant(t.db, t.store.tenant, T0))).toBe(4);
    expect(teamRows(t)).toBe(0);
    expect(unwrap(deleteTenant(t.db, t.store.tenant, T0))).toBe(0);
  });

  test("a whole tenant is busy while any thread of it runs", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    t.db.run(
      "INSERT INTO leases (branch_id, holder_id, epoch, expires_at) VALUES (?, 'w', 9, ?)",
      [branch(t, "researcher"), T0 + 1],
    );
    expect(code(deleteTenant(t.db, t.store.tenant, T0))).toBe("busy");
    untouched(t, 4);
  });
});

describe("deleting a thread with an effect in doubt", () => {
  test("is busy until the effect settles", () => {
    const c = loadCase("effect-crash-after-begin-idempotent");
    const { db, store } = caseStore(c);
    const log = unwrap(store.importLog(c.log ?? new Uint8Array()));
    const id = log.segments[0]?.header.thread_id ?? "";
    const refused = deleteThread(db, store.tenant, id, T0);
    expect(code(refused)).toBe("busy");
    expect(refused.ok ? "" : refused.error.message).toContain(
      "effect in doubt",
    );
  });
});
