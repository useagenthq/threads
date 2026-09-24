import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { type ThreadId, ThreadId as ThreadIdSchema } from "../../src/log";
import { deleteTenant, deleteThread } from "../../src/store/deletion";
import { code, T0, unwrap } from "../store/helpers";
import {
  liftTeamRefusal,
  relinked,
  STAGED,
  stagedLogs,
  storeLogs,
  type Team,
  teamStore,
  verified,
} from "./kit";

// The edges of a team deletion: every check fails closed, teams are found through their rows in
// this tenant, and nested teams, a member's subagent and a handoff target are deleted as §4.15
// says.

liftTeamRefusal();

const REBIND = "team-failed-rebind-bounces";
const ALL = ["lead", "researcher", "team", "writer"];
const OTHER_TEAM = "0192c000-0000-7000-8000-0000000000ee";
const Count = z.array(z.strictObject({ n: z.int() }));

function count(t: Team, sql: string, params: readonly string[] = []): number {
  return (
    Count.parse(t.db.all(`SELECT COUNT(*) AS n FROM ${sql}`, params))[0]?.n ?? 0
  );
}

const header = (t: Team, label: string) => {
  const h = t.logs.get(label)?.segments[0]?.header;
  if (h === undefined) throw new Error(`no log ${label}`);
  return h;
};
const thread = (t: Team, label: string): ThreadId => header(t, label).thread_id;

/** A spare single-thread log (thread ...0001) whose first event `edit` may relink. */
function spare(
  edit: (line: Record<string, unknown>) => Record<string, unknown>,
) {
  const bytes = new Uint8Array(
    readFileSync(join(STAGED, "legacy-wake-pending-row", "log.jsonl")),
  );
  return verified(relinked(bytes, edit));
}
const SPARE = ThreadIdSchema.parse("0192a000-0000-7000-8000-000000000001");

/** A second team's rows, led by `lead` in `tenant`: nothing in the logs names it. */
function rowsOnly(t: Team, lead: string, tenant: string): void {
  t.db.run(
    "INSERT INTO teams (team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at) VALUES (?, ?, ?, ?, NULL)",
    [OTHER_TEAM, tenant, lead, "0192b000-0000-7000-8000-0000000000ee"],
  );
  t.db.run(
    `INSERT INTO mail (mail_id, team_id, kind, to_name, to_generation, principal_key, root_request,
       envelope, created_at, state) VALUES ('m1', ?, 'message', 'x-1', 1, 'k', 'r', X'7B7D', 0, 'pending')`,
    [OTHER_TEAM],
  );
}

describe("every deletion check fails closed", () => {
  test("a branch whose log doesn't verify is busy, pointing at threads repair", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    t.db.run(
      "UPDATE events SET line = X'7B7D' WHERE branch_id = ? AND seq = 3",
      [header(t, "writer").branch_id],
    );
    const refused = deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0);
    expect(code(refused)).toBe("busy");
    expect(refused.ok ? "" : refused.error.message).toContain("threads repair");
    expect(count(t, "tombstones")).toBe(0);
  });

  test("a stored row that fails its schema refuses, never skips a delete", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    t.db.run("INSERT INTO threads (thread_id, tenant_id) VALUES ('junk', ?)", [
      t.store.tenant,
    ]);
    expect(code(deleteTenant(t.db, t.store.tenant, T0))).toBe("log_corrupt");
    expect(count(t, "threads")).toBe(5);
    expect(count(t, "tombstones")).toBe(0);
  });
});

describe("a doomed lead's teams are found through their rows, in this tenant", () => {
  test("a team whose teams row names a doomed lead goes, though no log names it", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    rowsOnly(t, thread(t, "writer"), t.store.tenant);
    unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0));
    expect(count(t, "teams")).toBe(0);
    expect(count(t, "mail")).toBe(0);
  });

  test("another tenant's team is never touched", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    rowsOnly(t, thread(t, "lead"), "other");
    unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0));
    expect(count(t, "teams")).toBe(1);
    expect(count(t, "mail")).toBe(1);
  });
});

describe("what goes with a thread, and what doesn't", () => {
  test("a nested team goes whole with the outer lead", () => {
    const t = teamStore(
      stagedLogs("team-nested-lead-rows", [
        "lead",
        "researcher",
        "researcher_team",
        "team",
      ]),
    );
    expect(count(t, "teams")).toBe(2);
    expect(
      unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(4);
    expect(count(t, "teams") + count(t, "team_members")).toBe(0);
  });

  test("a member's own subagent goes with the team", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    const writer = header(t, "writer");
    storeLogs(t.store, [
      spare((line) =>
        line["type"] === "thread_started"
          ? {
              ...line,
              data: {
                ...Object(line["data"]),
                parent: {
                  thread_id: writer.thread_id,
                  branch_id: writer.branch_id,
                  event_id: "0192e001-0000-7000-8000-000000000005",
                  relation: "subagent",
                },
              },
            }
          : line,
      ),
    ]);
    expect(
      unwrap(deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(5);
  });

  test("a lead that is a handoff target stays with its team", () => {
    const logs = stagedLogs(REBIND, ALL, (label, line) =>
      label === "lead" && line["type"] === "thread_started"
        ? {
            ...line,
            data: {
              ...Object(line["data"]),
              parent: {
                thread_id: SPARE,
                branch_id: "0192b000-0000-7000-8000-000000000001",
                event_id: "0192e001-0000-7000-8000-000000000003",
                relation: "handoff",
              },
            },
          }
        : line,
    );
    const t = teamStore(logs);
    storeLogs(t.store, [spare((line) => line)]);
    expect(unwrap(deleteThread(t.db, t.store.tenant, SPARE, T0))).toBe(1);
    expect(count(t, "threads")).toBe(4);
    expect(count(t, "teams")).toBe(1);
  });

  test("deleting a background child alone drops its parent's wake row for it", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    storeLogs(t.store, [spare((line) => line)]);
    t.db.run(
      "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)",
      [header(t, "lead").branch_id, SPARE],
    );
    unwrap(deleteThread(t.db, t.store.tenant, SPARE, T0));
    expect(count(t, "pending_wakes")).toBe(0);
  });

  test("a member whose lead is already gone can be deleted alone", () => {
    const t = teamStore(stagedLogs(REBIND, ALL));
    const lead = header(t, "lead");
    t.db.run("DELETE FROM events WHERE branch_id = ?", [lead.branch_id]);
    t.db.run("DELETE FROM branches WHERE branch_id = ?", [lead.branch_id]);
    t.db.run("DELETE FROM threads WHERE thread_id = ?", [lead.thread_id]);
    expect(
      unwrap(deleteThread(t.db, t.store.tenant, thread(t, "researcher"), T0)),
    ).toBe(1);
  });
});
