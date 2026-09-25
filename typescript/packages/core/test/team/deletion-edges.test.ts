import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { type ThreadId, ThreadId as ThreadIdSchema } from "../../src/log";
import { deleteTenant, deleteThread } from "../../src/store/deletion";
import { code, T0, unwrap } from "../store/helpers";
import {
  CASES,
  caseLogs,
  exec,
  query,
  relinked,
  storeLogs,
  type Team,
  teamStore,
  verified,
} from "./kit";

// The edges of a team deletion: every check fails closed, teams are found through their rows in
// this tenant, and nested teams, a member's subagent and a handoff target are deleted as §4.15
// says.

const REBIND = "team-failed-rebind-bounces";
const ALL = ["lead", "researcher", "team", "writer"];
const OTHER_TEAM = "0192c000-0000-7000-8000-0000000000ee";
/** `{}`: a blob that is no event line. */
const BRACES = new TextEncoder().encode("{}");
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
    readFileSync(join(CASES, "legacy-wake-pending-row", "log.jsonl")),
  );
  return verified(relinked(bytes, edit));
}
const SPARE = ThreadIdSchema.parse("0192a000-0000-7000-8000-000000000001");

/** A second team's rows, led by `lead` in `tenant`: nothing in the logs names it. */
async function rowsOnly(t: Team, lead: string, tenant: string): Promise<void> {
  await exec(
    t.db,
    "INSERT INTO teams (team_id, tenant_id, lead_thread_id, team_log_branch_id, closed_at) VALUES (?, ?, ?, ?, NULL)",
    [OTHER_TEAM, tenant, lead, "0192b000-0000-7000-8000-0000000000ee"],
  );
  await exec(
    t.db,
    `INSERT INTO mail (mail_id, team_id, kind, to_name, to_generation, principal_key, root_request,
       envelope, created_at, state) VALUES ('m1', ?, 'message', 'x-1', 1, 'k', 'r', ?, 0, 'pending')`,
    [OTHER_TEAM, BRACES],
  );
}

describe("every deletion check fails closed", () => {
  test("a branch whose log doesn't verify is busy, pointing at threads repair", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await exec(
      t.db,
      "UPDATE events SET line = ? WHERE branch_id = ? AND seq = 3",
      [BRACES, header(t, "writer").branch_id],
    );
    const refused = await deleteThread(
      t.db,
      t.store.tenant,
      thread(t, "lead"),
      T0,
    );
    expect(code(refused)).toBe("busy");
    expect(refused.ok ? "" : refused.error.message).toContain("threads repair");
    expect(await count(t, "tombstones")).toBe(0);
  });

  test("a stored row that fails its schema refuses, never skips a delete", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await exec(
      t.db,
      "INSERT INTO threads (thread_id, tenant_id) VALUES ('junk', ?)",
      [t.store.tenant],
    );
    expect(code(await deleteTenant(t.db, t.store.tenant, T0))).toBe(
      "log_corrupt",
    );
    expect(await count(t, "threads")).toBe(5);
    expect(await count(t, "tombstones")).toBe(0);
  });
});

describe("a doomed lead's teams are found through their rows, in this tenant", () => {
  test("a team whose teams row names a doomed lead goes, though no log names it", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await rowsOnly(t, thread(t, "writer"), t.store.tenant);
    unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0));
    expect(await count(t, "teams")).toBe(0);
    expect(await count(t, "mail")).toBe(0);
  });

  test("another tenant's team is never touched", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await rowsOnly(t, thread(t, "lead"), "other");
    unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0));
    expect(await count(t, "teams")).toBe(1);
    expect(await count(t, "mail")).toBe(1);
  });

  test("a team log that names another tenant's team never touches that team", async () => {
    const forged = caseLogs(REBIND, ALL, (label, line) =>
      label === "team" && line["type"] === "team_opened"
        ? { ...line, data: { ...Object(line["data"]), team: OTHER_TEAM } }
        : line,
    );
    const t = await teamStore(forged);
    await rowsOnly(t, "0192a000-0000-7000-8000-0000000000ef", "other");
    unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0));
    expect(await count(t, "teams WHERE team_id = ?", [OTHER_TEAM])).toBe(1);
    expect(await count(t, "mail WHERE team_id = ?", [OTHER_TEAM])).toBe(1);
  });
});

describe("what goes with a thread, and what doesn't", () => {
  test("a nested team goes whole with the outer lead", async () => {
    const t = await teamStore(
      caseLogs("team-nested-lead-feeds", [
        "inner",
        "lead",
        "researcher",
        "team",
      ]),
    );
    expect(await count(t, "teams")).toBe(2);
    expect(
      unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(4);
    expect((await count(t, "teams")) + (await count(t, "team_members"))).toBe(
      0,
    );
  });

  test("a member's own subagent goes with the team", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    const writer = header(t, "writer");
    await storeLogs(t.store, [
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
      unwrap(await deleteThread(t.db, t.store.tenant, thread(t, "lead"), T0)),
    ).toBe(5);
  });

  test("a lead that is a handoff target stays with its team", async () => {
    const logs = caseLogs(REBIND, ALL, (label, line) =>
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
    const t = await teamStore(logs);
    await storeLogs(t.store, [spare((line) => line)]);
    expect(unwrap(await deleteThread(t.db, t.store.tenant, SPARE, T0))).toBe(1);
    expect(await count(t, "threads")).toBe(4);
    expect(await count(t, "teams")).toBe(1);
  });

  test("deleting a background child alone drops its parent's wake row for it", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    await storeLogs(t.store, [spare((line) => line)]);
    await exec(
      t.db,
      "INSERT INTO pending_wakes (branch_id, child_thread_id) VALUES (?, ?)",
      [header(t, "lead").branch_id, SPARE],
    );
    unwrap(await deleteThread(t.db, t.store.tenant, SPARE, T0));
    expect(await count(t, "pending_wakes")).toBe(0);
  });

  test("a member whose lead is already gone can be deleted alone", async () => {
    const t = await teamStore(caseLogs(REBIND, ALL));
    const lead = header(t, "lead");
    await exec(t.db, "DELETE FROM events WHERE branch_id = ?", [
      lead.branch_id,
    ]);
    await exec(t.db, "DELETE FROM branches WHERE branch_id = ?", [
      lead.branch_id,
    ]);
    await exec(t.db, "DELETE FROM threads WHERE thread_id = ?", [
      lead.thread_id,
    ]);
    expect(
      unwrap(
        await deleteThread(t.db, t.store.tenant, thread(t, "researcher"), T0),
      ),
    ).toBe(1);
  });
});
