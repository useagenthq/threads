import { describe, expect, test } from "bun:test";
import { TeamId } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import type { SqliteDriver } from "../../src/store";
import { openBunSqlite } from "../../src/store/bun-sqlite";
import { TEAM_TABLES } from "../../src/store/deletion";
import { checkTeamLogs } from "../../src/team/cross";
import { changeRows, insertRows, turnOpeners } from "../../src/team/index";
import { rebuildTeamIndex } from "../../src/team/rebuild";
import { code, fixture, unwrap } from "../store/helpers";
import {
  appendable,
  caseLogs,
  storeLogs,
  TENANT,
  type Team,
  teamIndexRows,
  teamStore,
  verified,
} from "./kit";

// The replay rule by construction: the rows a writer leaves, appending each event with
// insertRows then changeRows in the order the team's appends happened, equal the rows a rebuild
// folds from the logs alone. And rule 43, forged one clause at a time.

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
        e !== undefined && appendable(t.db, e);
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
      const t = teamStore(caseLogs(name, labels));
      const rebuilt = rowsOf(t);
      written(t);
      expect(rowsOf(t)).toEqual(rebuilt);
    });

  test("a rebuild is idempotent and starts a new feed epoch", () => {
    const t = teamStore(
      caseLogs("team-settle-wakes-lead", CASES["team-settle-wakes-lead"] ?? []),
    );
    const first = teamIndexRows(t.db, [t.team], branches(t));
    unwrap(rebuildTeamIndex(t.store, t.team));
    expect(teamIndexRows(t.db, [t.team], branches(t))).toEqual(first);
    expect(t.db.all("SELECT DISTINCT epoch FROM team_feed", [])).toEqual([
      { epoch: 2 },
    ]);
  });

  test("a rebuild refuses a bounce of another run, and nested own-team mail in a task turn", () => {
    for (const [name, labels] of [
      [
        "team-bounce-provenance-rejected",
        ["lead", "researcher", "team", "writer"],
      ],
      [
        "team-nested-task-turn-other-run-rejected",
        ["inner", "lead", "researcher", "team"],
      ],
    ] as const) {
      const { store } = fixture(TENANT);
      storeLogs(
        store,
        [...caseLogs(name, labels).values()].map((b) => verified(b)),
      );
      const outer = TeamId.parse("0192c000-0000-7000-8000-000000000001");
      expect(code(rebuildTeamIndex(store, outer))).toBe("invalid_transition");
    }
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
    const t = teamStore(caseLogs(SETTLE, CASES[SETTLE] ?? []), TENANT, spy);
    reads.length = 0;
    unwrap(rebuildTeamIndex(t.store, t.team));
    expect(reads.length).toBeGreaterThan(0);
    expect(reads.every((inside) => inside)).toBe(true);
  });

  test("a team no lead names is not_found", () => {
    const t = teamStore(caseLogs("team-settle-wakes-lead", ["lead", "team"]));
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
    [...caseLogs(SETTLE, labels, edit).values()].map((bytes) => {
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
