// A crash-drill host process for runs started through the run API. `bun api-worker.ts api <dir>`
// runs a host on the drill's file-backed store. With DRILL_START=1 it starts one API run; without
// it, it starts nothing and only recovers what the store holds. The agent calls `charge` (a host
// tool with an effect) when DRILL_CHARGE=1, then answers "done". It stops once the run's branch
// settles: its turn closed, or parked. DRILL_STOP_AT, DRILL_GO and the JSONL records work as in
// worker.ts; `charge` records each call in charges.jsonl.

import { existsSync } from "node:fs";
import { join } from "node:path";
import { agent, type Model, scriptedModel, sqlite, tool } from "@threads/core";
import {
  BranchId,
  knownEvents,
  openStore,
  storeConnection,
  tenantStore,
  type VerifiedLog,
} from "@threads/core/host";
import { z } from "zod";
import { host } from "../../src";
import { reached, record, until } from "./worker";

export const TENANT = "acme";
const USER = { issuer: "api", tenant: TENANT, subject: "alice" };
const usage = { input_tokens: 1, output_tokens: 1 };
const CHARGE = {
  content: [{ type: "tool_use", call_id: "c1", name: "charge", input: {} }],
  stop_reason: "tool_use",
  usage,
};
const DONE = {
  content: [{ type: "text", text: "done" }],
  stop_reason: "end_turn",
  usage,
};

/** The drill run's branch, read back from the log; undefined before the run is durable. */
async function branchLog(
  store: ReturnType<typeof sqlite>,
): Promise<VerifiedLog | undefined> {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.strictObject({ branch_id: BranchId }))
    .parse(db.all("SELECT branch_id FROM run_receipts LIMIT 1", []));
  if (row === undefined) return undefined;
  const { log } = await openStore(tenantStore(store, TENANT));
  const read = log.read(row.branch_id);
  return read.ok ? read.value : undefined;
}

function model(where: string, script: readonly unknown[]): Model {
  const inner = scriptedModel({ responses: [...script] });
  return {
    ...inner,
    send: (request, context) => {
      record(where, "model.jsonl", {});
      reached("model_request", where);
      return inner.send(request, context);
    },
  };
}

async function serve(where: string): Promise<void> {
  const store = sqlite(where);
  const before = await branchLog(store);
  // A restart answers only what the log has not: the script is the model's, not the process's.
  const answered =
    before === undefined
      ? 0
      : knownEvents(before).filter((e) => e.type === "model_response").length;
  const script = process.env["DRILL_CHARGE"] === "1" ? [CHARGE, DONE] : [DONE];
  const charge = tool({
    name: "charge",
    description: "Charge the card.",
    input: z.object({}),
    runs: "host",
    execute: async () => {
      record(where, "charges.jsonl", {});
      reached("effect_begin", where);
      return "charged";
    },
  });
  const bot = agent({
    name: "bot",
    model: model(where, script.slice(answered)),
    tools: [charge],
    permissions: { allow: ["charge"] },
  });
  const h = host({ store, agents: { bot } });
  await h.ready();
  if (process.env["DRILL_START"] === "1") {
    const started = await h.startRun(
      { agent: "bot", input: "Charge me." },
      { principal: USER, idempotencyKey: "drill" },
    );
    if (!started.ok) throw new Error(started.error.message);
  }
  await until(async () => {
    const read = await branchLog(store);
    return (
      read !== undefined && (!read.fold.turnOpen || read.fold.parked.length > 0)
    );
  });
  await h.stop();
}

if (import.meta.main) {
  const [, where] = process.argv.slice(2);
  if (where === undefined) throw new Error("usage: api-worker.ts api <dir>");
  if (process.env["DRILL_GO"] === "1")
    while (!existsSync(join(where, "go"))) Bun.sleepSync(1);
  await serve(where);
  process.exit(0);
}
