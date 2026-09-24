import { describe, expect, test } from "bun:test";
import { z } from "zod";
import { type KnownEvent, TeamId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { SqliteDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { TEAM_TABLES } from "../../src/store/deletion";
import { checkTeamLogs } from "../../src/team/cross";
import { changeRows, insertRows, turnOpeners } from "../../src/team/index";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { code, unwrap } from "../store/helpers";
import {
  liftTeamRefusal,
  stagedLogs,
  TENANT,
  type Team,
  teamIndexRows,
  teamStore,
  verified,
} from "./kit";

// The replay rule by construction: the rows a writer leaves, appending each event with
// insertRows then changeRows in the order the team's appends happened, equal the rows a rebuild
// folds from the logs alone. And rule 43, forged one clause at a time.

liftTeamRefusal();

const CASES: Readonly<Record<string, readonly string[]>> = {
  "team-settle-wakes-lead": ["lead", "researcher", "team"],
  "team-failed-rebind-bounces": ["lead", "researcher", "team", "writer"],
  "team-operator-start-and-wait": ["lead", "researcher", "team"],
  "team-tree-starting-member-pending": ["lead", "team"],
};

const branches = (t: Team) =>
  [...t.logs.values()].flatMap((l) => l.segments[0]?.header.branch_id ?? []);

function rowsOf(t: Team) {
  const { team_feed: _feed, ...rest } = teamIndexRows(
    t.db,
    [t.team],
    branches(t),
  );
  return rest;
}

const Exists = z.array(z.strictObject({ n: z.int() }));

function has(t: Team, table: string, key: string, id: string): boolean {
  const rows = Exists.parse(
    t.db.all(`SELECT COUNT(*) AS n FROM ${table} WHERE ${key} = ?`, [id]),
  );
  return (rows[0]?.n ?? 0) > 0;
}

/** Whether `e`'s append could have happened yet: the rows it moves exist (causal order). */
function ready(t: Team, e: KnownEvent): boolean {
  if (e.type === "message_received" || e.type === "mail_refused")
    return has(t, "mail", "mail_id", e.data.mail_id);
  if (e.type === "user_input" && e.data.mail_id !== undefined)
    return has(t, "mail", "mail_id", e.data.mail_id);
  if (e.type === "ask_closed") return has(t, "asks", "ask_id", e.data.ask_id);
  if (e.type === "thread_started" && e.data.parent?.relation === "team_member")
    return has(t, "team_members", "thread_id", e.thread_id);
  if (e.type === "operator_request")
    return has(t, "teams", "team_log_branch_id", e.branch_id);
  return true;
}

/**
 * Wipes the team's rows, then appends every event as the team's writers would: one log at a
 * time, each event once the rows it moves exist, insertRows then changeRows.
 */
function written(t: Team): void {
  for (const table of TEAM_TABLES.filter((t) => t !== "team_feed"))
    t.db.run(`DELETE FROM ${table} WHERE team_id = ?`, [t.team]);
  const queues = [...t.logs.values()].map((chain) => {
    const header = chain.segments[0]?.header;
    if (header === undefined) throw new Error("no header");
    const log = { threadId: header.thread_id, branchId: header.branch_id };
    return {
      log,
      opened: turnOpeners(chain.events),
      events: [...knownEvents(chain)],
    };
  });
  let moved = true;
  while (moved) {
    moved = false;
    for (const q of queues)
      for (
        let e = q.events[0];
        e !== undefined && ready(t, e);
        e = q.events[0]
      ) {
        q.events.shift();
        insertRows(t.db, q.log, [e]);
        changeRows(t.db, q.log, [e], q.opened);
        moved = true;
      }
  }
  expect(queues.every((q) => q.events.length === 0)).toBe(true);
}

describe("the replay rule", () => {
  for (const [name, labels] of Object.entries(CASES))
    test(`${name}: appends and a rebuild leave the same rows`, () => {
      const t = teamStore(stagedLogs(name, labels));
      const rebuilt = rowsOf(t);
      written(t);
      expect(rowsOf(t)).toEqual(rebuilt);
    });

  test("a rebuild is idempotent and starts a new feed epoch", () => {
    const t = teamStore(
      stagedLogs(
        "team-settle-wakes-lead",
        CASES["team-settle-wakes-lead"] ?? [],
      ),
    );
    const first = teamIndexRows(t.db, [t.team], branches(t));
    unwrap(rebuildTeamIndex(t.store, t.team));
    expect(teamIndexRows(t.db, [t.team], branches(t))).toEqual(first);
    expect(t.db.all("SELECT DISTINCT epoch FROM team_feed", [])).toEqual([
      { epoch: 2 },
    ]);
  });

  test("a rebuild reads the logs inside the transaction that refolds them", () => {
    const base = openBunSqlite(":memory:");
    let depth = 0;
    const reads: boolean[] = [];
    const spy: SqliteDriver = {
      ...base,
      transaction: (fn) =>
        base.transaction(() => {
          depth += 1;
          try {
            return fn();
          } finally {
            depth -= 1;
          }
        }),
      all: (sql, params) => {
        if (sql.includes("FROM events")) reads.push(depth > 0);
        return base.all(sql, params);
      },
    };
    const t = teamStore(stagedLogs(SETTLE, CASES[SETTLE] ?? []), TENANT, spy);
    reads.length = 0;
    unwrap(rebuildTeamIndex(t.store, t.team));
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.every((inside) => inside)).toBe(true);
  });

  test("a team no lead names is not_found", () => {
    const t = teamStore(stagedLogs("team-settle-wakes-lead", ["lead", "team"]));
    const other = TeamId.parse("0192c000-0000-7000-8000-0000000000ff");
    expect(code(rebuildTeamIndex(t.store, other))).toBe("not_found");
  });
});

type Line = Record<string, unknown>;
const data = (line: Line): Line => Object(line["data"]);
const SETTLE = "team-settle-wakes-lead";

/** Rule 43 over a staged team whose lines `edit` forged, re-chained so each log still verifies. */
function cross(edit: (label: string, line: Line) => Line) {
  const labels = CASES[SETTLE] ?? [];
  return checkTeamLogs(
    [...stagedLogs(SETTLE, labels, edit).values()].map((bytes) => {
      const chain = verified(bytes);
      const header = chain.segments[0]?.header;
      if (header === undefined) throw new Error("no header");
      return {
        threadId: header.thread_id,
        branchId: header.branch_id,
        events: knownEvents(chain),
      };
    }),
  );
}

const inData =
  (type: string, label: string, change: (d: Line) => Line) =>
  (at: string, line: Line): Line =>
    at === label && line["type"] === type
      ? { ...line, data: change(data(line)) }
      : line;

describe("rule 43", () => {
  test("the staged team passes", () => {
    expect(cross((_, line) => line)).toBeUndefined();
  });

  test("a mail sent from a log its from doesn't name", () => {
    const from = (d: Line): Line => {
      const env = Object(d["envelope"]);
      return {
        ...d,
        envelope: {
          ...env,
          from: { ...Object(env["from"]), name: "writer-1" },
        },
      };
    };
    const forged = cross((label, line) =>
      inData(
        "message_received",
        "lead",
        from,
      )(label, inData("message_sent", "researcher", from)(label, line)),
    );
    expect(forged?.message).toContain("`from` names");
  });

  test("a receipt in a log its to doesn't name", () => {
    const to = (d: Line): Line => {
      const env = Object(d["envelope"]);
      return {
        ...d,
        envelope: { ...env, to: { name: "writer-1", generation: 1 } },
      };
    };
    const forged = cross((label, line) =>
      inData(
        "message_received",
        "lead",
        to,
      )(label, inData("message_sent", "researcher", to)(label, line)),
    );
    expect(forged?.message).toContain("`to` names");
  });

  test("a member whose parent is not its member_started's", () => {
    const forged = cross(
      inData("thread_started", "researcher", (d) => ({
        ...d,
        parent: {
          ...Object(d["parent"]),
          event_id: "0192e001-0000-7000-8000-0000000000ff",
        },
      })),
    );
    expect(forged?.message).toContain("parent");
  });

  test("a task taken by another principal than its mail's", () => {
    const forged = cross((label, line) =>
      label === "researcher" && line["type"] === "user_input"
        ? {
            ...line,
            actor: {
              kind: "host",
              principal: { issuer: "api", tenant: "acme", subject: "mallory" },
            },
          }
        : line,
    );
    expect(forged?.message).toContain("principal");
  });
});
