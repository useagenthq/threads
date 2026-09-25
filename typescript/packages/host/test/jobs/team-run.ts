import { agent, LogStore, scriptedModel } from "@threads/core";
import { BranchId, hostRunner, storeOf, ThreadId } from "@threads/core/host";
import { z } from "zod";
import { beforeRun, sqlAll } from "../sql";
import { drillDriver } from "./stores";
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
async function store(where: string, stop?: string) {
  const { db: base, artifacts } = await drillDriver(where);
  const db = beforeRun(base, (sql, params) => {
    const settles =
      sql.includes("UPDATE team_members SET result") &&
      params.some(
        (p) =>
          p instanceof Uint8Array && decoder.decode(p).includes("researcher-1"),
      );
    if (stop !== undefined && settles) reached(stop, where);
  });
  const log = await LogStore.open(db, Date.now, artifacts);
  if (!log.ok) throw new Error(log.error.message);
  return storeOf({ log: log.value, artifacts });
}

async function run(where: string): Promise<string> {
  const r = await lead(true).run("Work.", {
    store: await store(where, "member_settle"),
  });
  return r.status;
}

async function resume(where: string): Promise<string> {
  const db = (await drillDriver(where)).db;
  const [row] = z
    .array(z.object({ lead_thread_id: ThreadId }))
    .parse(await sqlAll(db, "SELECT lead_thread_id FROM teams", []));
  const branch = z
    .array(z.object({ branch_id: z.string() }))
    .parse(
      await sqlAll(db, "SELECT branch_id FROM branches WHERE thread_id = ?", [
        row?.lead_thread_id ?? "",
      ]),
    )[0]?.branch_id;
  await db.close();
  if (row === undefined || branch === undefined) throw new Error("no lead");
  const opened = await store(where);
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
