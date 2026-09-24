// A lead running its team, as a real child process on one file-backed store, for the team run
// drills (team.test.ts). `bun team-run.ts <role> <dir>`:
//
// - run: a lead starts a researcher, answers, and wakes for its result. With
//   DRILL_STOP_AT=member_settle it prints `at member_settle` and blocks inside the researcher's
//   settling append (its member_idle), until the parent kills it.
// - resume: the restart: the host's recovery of the open run, no new input. Prints the run's
//   status.

import { join } from "node:path";
import {
  agent,
  fileArtifacts,
  LogStore,
  type SqliteDriver,
  scriptedModel,
} from "@threads/core";
import { openBunSqlite } from "@threads/core/bun-sqlite";
import { BranchId, hostRunner, storeOf, ThreadId } from "@threads/core/host";
import { z } from "zod";
import { reached } from "./worker";

const usage = { input_tokens: 10, output_tokens: 2 };
const say = (text: string) => ({
  content: [{ type: "text", text }],
  stop_reason: "end_turn",
  usage,
});
const start = {
  content: [
    {
      type: "tool_use",
      call_id: "c1",
      name: "start",
      input: { agent: "researcher", task: "Research." },
    },
  ],
  stop_reason: "tool_use",
  usage,
};
const decoder = new TextDecoder();

/** The lead and its team; a fresh process starts the script over from `from`. */
function lead(fresh: boolean) {
  return agent({
    name: "lead",
    model: scriptedModel({
      responses: [
        ...(fresh ? [start] : []),
        say("Started."),
        say("Final."),
        say("Final."),
      ],
    }),
    team: [
      agent({
        name: "researcher",
        model: scriptedModel({
          responses: [say("Found it."), say("Found it.")],
        }),
      }),
    ],
  });
}

/** The drill store; with a stop point, a driver that blocks inside the researcher's settlement. */
function store(where: string, stop?: string) {
  const base = openBunSqlite(join(where, "threads.db"));
  const db: SqliteDriver = {
    ...base,
    run: (sql, params) => {
      const settles =
        sql.includes("UPDATE team_members SET result") &&
        params.some(
          (p) =>
            p instanceof Uint8Array &&
            decoder.decode(p).includes("researcher-1"),
        );
      if (stop !== undefined && settles) reached(stop, where);
      base.run(sql, params);
    },
  };
  const artifacts = fileArtifacts(join(where, "artifacts"));
  const log = LogStore.open(db, Date.now, artifacts);
  if (!log.ok) throw new Error(log.error.message);
  return storeOf({ log: log.value, artifacts });
}

async function run(where: string): Promise<string> {
  const r = await lead(true).run("Work.", {
    store: store(where, "member_settle"),
  });
  return r.status;
}

async function resume(where: string): Promise<string> {
  const db = openBunSqlite(join(where, "threads.db"));
  const [row] = z
    .array(z.object({ lead_thread_id: ThreadId }))
    .parse(db.all("SELECT lead_thread_id FROM teams", []));
  const branch = z
    .array(z.object({ branch_id: z.string() }))
    .parse(
      db.all("SELECT branch_id FROM branches WHERE thread_id = ?", [
        row?.lead_thread_id ?? "",
      ]),
    )[0]?.branch_id;
  db.close();
  if (row === undefined || branch === undefined) throw new Error("no lead");
  const opened = store(where);
  const restarted = lead(false);
  const runner = hostRunner(restarted);
  if (runner === undefined) throw new Error("agent() registers a host runner");
  const result = await runner.execute(
    {
      store: opened,
      principal: { issuer: "api", tenant: "local", subject: "operator" },
      thread: {
        id: row.lead_thread_id,
        branch: BranchId.parse(branch),
        store: opened,
      },
    },
    [],
  );
  return result.status;
}

if (import.meta.main) {
  const [role = "", where] = process.argv.slice(2);
  if (where === undefined) throw new Error("usage: team-run.ts <role> <dir>");
  const said = role === "run" ? await run(where) : await resume(where);
  process.stdout.write(`${said}\n`);
  process.exit(0);
}
