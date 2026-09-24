// A store process for the team drills (team.test.ts), run as a real child process on one
// file-backed store. `bun team-worker.ts <role> <dir>`:
//
// - lead: opens a lead's log with its first append (thread_started{team}, user_input), which opens
//   the team log, the teams row and the lead's row in the same transaction. With
//   DRILL_STOP_AT=lead_first_append it prints `at lead_first_append` and blocks mid-transaction,
//   at the first feed row, until the parent kills it.
// - open-a, open-b: after <dir>/go exists, runs branch.open on one shared branch, each with its
//   own thread id and holder, and prints `opened` or `already_open`.

import { existsSync } from "node:fs";
import { join } from "node:path";
import {
  type EventDraft,
  LogStore,
  memoryArtifacts,
  type SqliteDriver,
} from "@threads/core";
import { openBunSqlite } from "@threads/core/bun-sqlite";
import { BranchId, ThreadId } from "@threads/core/host";
import { reached } from "./worker";

export const TENANT = "acme";
export const TEAM = "0192c000-0000-7000-8000-000000000001";
export const LEAD_BRANCH: BranchId = BranchId.parse(
  "0192b000-0000-7000-8000-0000000000b1",
);
export const TEAM_LOG: BranchId = BranchId.parse(
  "0192b000-0000-7000-8000-0000000000b3",
);
/** The branch both open-* workers race to open. */
export const RACED: BranchId = BranchId.parse(
  "0192b000-0000-7000-8000-0000000000c1",
);
/** Each open-* worker's own thread for the raced branch. */
export const RACER_THREAD: Readonly<Record<string, ThreadId>> = {
  "open-a": ThreadId.parse("0192a000-0000-7000-8000-0000000000ca"),
  "open-b": ThreadId.parse("0192a000-0000-7000-8000-0000000000cb"),
};

const started = (team?: { id: string; log_branch_id: string }): EventDraft => ({
  type: "thread_started",
  type_version: 1,
  critical: true,
  actor: { kind: "host" },
  data: {
    agent_name: "lead",
    config_hash: "a".repeat(64),
    model: { provider: "scripted", name: "scripted-1" },
    model_params: { max_tokens: 1024 },
    adapter: { name: "scripted", version: "1", settings: {} },
    instructions: "You lead.",
    tools: [],
    ...(team === undefined ? {} : { team }),
  },
});

const input = (text: string): EventDraft => ({
  type: "user_input",
  type_version: 1,
  critical: true,
  actor: {
    kind: "user",
    principal: { issuer: "api", tenant: TENANT, subject: "alice" },
  },
  data: { source: "api", text },
});

/** The drill store; with a stop point, a driver that blocks at the first feed row. */
export function drillStore(where: string, stop?: string): LogStore {
  const base = openBunSqlite(join(where, "threads.db"));
  const db: SqliteDriver = {
    ...base,
    run: (sql, params) => {
      if (stop !== undefined && sql.includes("INSERT INTO team_feed"))
        reached(stop, where);
      base.run(sql, params);
    },
  };
  const store = LogStore.open(db, Date.now, memoryArtifacts(), TENANT);
  if (!store.ok) throw new Error(store.error.message);
  return store.value;
}

function lead(where: string): string {
  const store = drillStore(where, "lead_first_append");
  const opened = store.openBranch({
    threadId: ThreadId.parse("0192a000-0000-7000-8000-0000000000b1"),
    branchId: LEAD_BRANCH,
    lease: { holderId: "lead", ttlMs: 0 },
    drafts: [
      started({ id: TEAM, log_branch_id: TEAM_LOG }),
      input("Lead the team."),
    ],
  });
  if (!opened.ok) throw new Error(opened.error.message);
  return "opened";
}

function open(role: string, where: string): string {
  const thread = RACER_THREAD[role];
  if (thread === undefined) throw new Error(`no racer ${role}`);
  while (!existsSync(join(where, "go"))) Bun.sleepSync(1);
  const opened = drillStore(where).openBranch({
    threadId: thread,
    branchId: RACED,
    lease: { holderId: role, ttlMs: 60_000 },
    drafts: [started(), input(role)],
  });
  if (!opened.ok) throw new Error(opened.error.message);
  return opened.value === "already_open" ? "already_open" : "opened";
}

if (import.meta.main) {
  const [role = "", where] = process.argv.slice(2);
  if (where === undefined)
    throw new Error("usage: team-worker.ts <role> <dir>");
  process.stdout.write(
    `${role === "lead" ? lead(where) : open(role, where)}\n`,
  );
  process.exit(0);
}
