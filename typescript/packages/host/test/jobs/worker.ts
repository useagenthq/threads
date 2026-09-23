// A crash-drill host process (F9.6, F10.5). The drills beside it
// run it as a real child process on one file-backed SQLite store and kill it with SIGKILL.
// `bun worker.ts <role> <dir>`:
//
// - serve: a host with one fake channel. With DRILL_WEBHOOK=1 it delivers the drill's one webhook
//   (a redelivery after a restart). The agent answers "done" and the host sends that reply as a
//   channel_send effect. It stops once the conversation settles: replied, or parked.
// - schedule: fires every occurrence in DRILL_OCCURRENCES of one schedule, then waits until each
//   occurrence was claimed and no run is in flight, in whichever process ran it.
//
// With DRILL_GO=1 it first waits for <dir>/go, so two workers start together. At the point named
// by DRILL_STOP_AT it prints `at <point>` and blocks its event loop, lease renewal included, until
// the parent kills it or creates <dir>/release. Every send, refusal, model request and ack is a
// JSONL line in <dir> with the pid that made it.

import {
  closeSync,
  existsSync,
  fsyncSync,
  openSync,
  readFileSync,
  writeSync,
} from "node:fs";
import { join } from "node:path";
import {
  agent,
  type ChannelAdapter,
  extension,
  type Model,
  type Store,
  scriptedModel,
  sqlite,
} from "@threads/core";
import { sandboxFetch } from "@threads/core/adapter";
import {
  knownEvents,
  openStore,
  storeConnection,
  ThreadId,
  tenantStore,
  type VerifiedLog,
} from "@threads/core/host";
import { z } from "zod";
import { host } from "../../src";
import { HostContext } from "../../src/context";
import { bindSchedules, tick } from "../../src/schedules";

export const TEAM = "T1";
const USER = { issuer: "fake:T1", tenant: TEAM, subject: "U1" };
const DONE = {
  content: [{ type: "text", text: "done" }],
  stop_reason: "end_turn",
  usage: { input_tokens: 1, output_tokens: 1 },
};
const SETTLE_MS = 20_000;
const Row = z.record(z.string(), z.unknown());

/** A scripted point: blocks the whole process, event loop included, like a stalled host. */
function reached(point: string, where: string): void {
  if (process.env["DRILL_STOP_AT"] !== point) return;
  writeSync(1, `at ${point}\n`);
  while (!existsSync(join(where, "release"))) Bun.sleepSync(10);
}

/** One durable line: a recorded send must survive this process being killed. */
function record(where: string, file: string, row: object): void {
  const fd = openSync(join(where, file), "a");
  try {
    writeSync(fd, `${JSON.stringify({ ...row, pid: process.pid })}\n`);
    fsyncSync(fd);
  } finally {
    closeSync(fd);
  }
}

export function rows(
  where: string,
  file: string,
): readonly Record<string, unknown>[] {
  const path = join(where, file);
  if (!existsSync(path)) return [];
  return readFileSync(path, "utf8")
    .split("\n")
    .filter((line) => line !== "")
    .map((line) => Row.parse(JSON.parse(line)));
}

export function webhookBody(): string {
  return JSON.stringify([
    {
      kind: "message",
      principal: USER,
      address: "C1",
      item_key: "m1",
      content: "hello",
    },
  ]);
}

/** A fake provider whose sends are lines of <dir>/sends.jsonl, fenced at its transport. */
function fileChannel(where: string, lookups: string): ChannelAdapter {
  const Items = z.array(z.unknown());
  return {
    agent: "bot",
    capabilities: {
      lookup: lookups === "none" ? "none" : "final",
      buttons: false,
      edits: false,
      files: false,
      direct_messages: false,
    },
    limits: {},
    secrets: {},
    verify: (raw) => ({
      ok: true,
      value: {
        tenant: TEAM,
        installation_id: TEAM,
        delivery_id: raw.headers["delivery"] ?? "",
      },
    }),
    // The host parses each item as Inbound: the fake passes them through.
    parse: (raw) => ({
      ok: true,
      value: z
        .array(z.custom<never>())
        .parse(Items.parse(JSON.parse(new TextDecoder().decode(raw.body)))),
    }),
    ack: () => ({ status: 200, headers: {}, body: new Uint8Array() }),
    render: (event) =>
      event.type === "model_response"
        ? [
            {
              text: event.data.content
                .map((p) => (p.type === "text" ? p.text : ""))
                .join(""),
            },
          ]
        : [],
    perform: async (op, key) => {
      reached("effect_begin", where);
      // The transport: the host's fence is checked where the request would leave.
      const send = sandboxFetch(async () => {
        record(where, "sends.jsonl", { key, text: op["text"] });
        return new Response();
      });
      try {
        await send("fake://send");
      } catch (error) {
        record(where, "refused.jsonl", { key });
        throw error;
      }
      // The provider has it; its receipt is not yet the host's.
      reached("sent", where);
      return { status: "sent", platform_ref: `ref-${key}` };
    },
    lookup: async (key) => {
      if (lookups === "unknown")
        return { status: "unknown", reason: "the provider did not answer" };
      const sent = rows(where, "sends.jsonl").some((r) => r["key"] === key);
      return sent
        ? { status: "found", value: `ref-${key}` }
        : { status: "not_found" };
    },
  };
}

/** The drill conversation's main branch, read back from the log. */
export async function conversation(
  store: Store,
): Promise<VerifiedLog | undefined> {
  const { db } = await storeConnection(store);
  const [first] = z
    .array(z.strictObject({ thread_id: ThreadId }))
    .parse(db.all("SELECT thread_id FROM inbox LIMIT 1", []));
  if (first === undefined) return undefined;
  const { log } = await openStore(tenantStore(store, TEAM));
  const main = log.mainBranch(first.thread_id);
  const read = main.ok ? log.read(main.value) : undefined;
  return read?.ok === true ? read.value : undefined;
}

/** Replied (the host's reply to the final response has its result) or parked. */
export function settled(read: VerifiedLog | undefined): boolean {
  if (read === undefined) return false;
  const { fold } = read;
  const replies = [...fold.calls.entries()].filter(([id]) =>
    id.startsWith("send_"),
  );
  return (
    fold.parked.length > 0 ||
    (replies.length > 0 && replies.every(([, c]) => c.result !== undefined))
  );
}

function scripted(where: string, answers: number): Model {
  const model = scriptedModel({ responses: Array(answers).fill(DONE) });
  return {
    ...model,
    send: (request, context) => {
      record(where, "model.jsonl", {});
      reached("model_request", where);
      return model.send(request, context);
    },
  };
}

async function until(probe: () => Promise<boolean>): Promise<void> {
  const deadline = Date.now() + SETTLE_MS;
  while (!(await probe())) {
    if (Date.now() > deadline) throw new Error("the drill never settled");
    await Bun.sleep(20);
  }
}

async function serve(where: string): Promise<void> {
  const store = sqlite(where);
  const before = await conversation(store);
  // A restart answers only what the log has not: the script is the model's, not the process's.
  const answered =
    before !== undefined &&
    knownEvents(before).some((e) => e.type === "model_response");
  const drill = extension({
    name: "drill",
    hooks: {
      // before_model runs once the turn's user_input is durable, before its model_request.
      beforeModel: async () => {
        reached("user_input", where);
        return { decision: "proceed" };
      },
    },
  });
  const bot = agent({
    name: "bot",
    model: scripted(where, answered ? 0 : 1),
    extensions: [drill],
  });
  const lookups = process.env["DRILL_LOOKUP"] ?? "final";
  const h = host({
    store,
    agents: { bot },
    channels: { fake: fileChannel(where, lookups) },
  });
  await h.ready();
  if (process.env["DRILL_WEBHOOK"] === "1") {
    const answer = await h.fetch(
      new Request("http://drill/channels/fake/events", {
        method: "POST",
        headers: { delivery: "d1" },
        body: webhookBody(),
      }),
    );
    record(where, "acks.jsonl", { status: answer.status });
    reached("webhook_ack", where);
  }
  await until(async () => {
    const read = await conversation(store);
    // The host appends a reply's effect_commit with its tool_result, outside any run's
    // observers: seen in the log, the commit is durable.
    if (
      read !== undefined &&
      knownEvents(read).some((e) => e.type === "effect_commit")
    )
      reached("effect_commit", where);
    return settled(read);
  });
  await h.stop();
}

async function schedule(where: string): Promise<void> {
  const at = z
    .array(z.int())
    .parse(JSON.parse(process.env["DRILL_OCCURRENCES"] ?? "[]"));
  const store = sqlite(where);
  const model = scripted(where, at.length);
  const ctx = new HostContext(
    store,
    { bot: agent({ name: "bot", model }) },
    {},
  );
  const bound = bindSchedules(ctx, [
    { id: "tick", agent: "bot", cron: "* * * * *", input: "Tick." },
  ]);
  if (typeof bound === "string") throw new Error(bound);
  const startedAt = (at[0] ?? 0) - 1;
  let now = startedAt;
  // Like a host's tick loop: every pass fires what is due at `now` and resumes a run whose
  // input is durable but that never went.
  const ticked = async (done: () => Promise<boolean>): Promise<void> =>
    until(async () => {
      await tick(ctx, bound, startedAt, now);
      return done();
    });
  for (const occurrence of at) {
    // Each occurrence waits out the last one's run, so few are skipped as overlaps.
    await ticked(async () => !(await turnOpen(store)));
    now = occurrence + 1;
  }
  await ticked(
    async () =>
      (await claimed(store)) === at.length && !(await turnOpen(store)),
  );
  await ctx.stop();
}

async function claimed(store: Store): Promise<number> {
  const { db } = await storeConnection(store);
  return db.all("SELECT 1 FROM schedule_occurrences", []).length;
}

async function turnOpen(store: Store): Promise<boolean> {
  const { db } = await storeConnection(store);
  const [row] = z
    .array(z.strictObject({ thread_id: ThreadId }))
    .parse(
      db.all("SELECT DISTINCT thread_id FROM schedule_occurrences LIMIT 1", []),
    );
  if (row === undefined) return false;
  const { log } = await openStore(tenantStore(store, "local"));
  const main = log.mainBranch(row.thread_id);
  const read = main.ok ? log.read(main.value) : undefined;
  return read?.ok === true && read.value.fold.turnOpen;
}

if (import.meta.main) {
  const [role, where] = process.argv.slice(2);
  if (where === undefined) throw new Error("usage: worker.ts <role> <dir>");
  if (process.env["DRILL_GO"] === "1")
    while (!existsSync(join(where, "go"))) Bun.sleepSync(1);
  await (role === "serve" ? serve(where) : schedule(where));
  process.exit(0);
}
