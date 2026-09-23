import { afterEach, describe, expect, test } from "bun:test";
import {
  agent,
  type Model,
  type Store,
  scriptedModel,
  sqlite,
  tool,
} from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  type BranchId,
  type EventId,
  type KnownEvent,
  knownEvents,
  openStore,
  type Principal,
  storeConnection,
  type ThreadId,
  tenantStore,
  type VerifiedLog,
} from "@threads/core/host";
import { z } from "zod";
import { type Host, host } from "../src";
import { hostTicked } from "../src/host";
import { alice, authenticate, eve, mailer, say, until, use } from "./kit";

// A run started through the run API resumes when the host that ran it stalls or dies: the next
// host's recovery pass runs its open turn from the log with no new input. The "crash" is a host
// whose model or tool never answers and is never stopped; its lease is expired, as the drills
// do, and a new host starts on the same store.

const hosts: Host[] = [];
const gates: (() => void)[] = [];
afterEach(async () => {
  for (const open of gates.splice(0)) open();
  for (const h of hosts.splice(0)) await h.stop();
});

type Run = {
  readonly tenant: string;
  readonly thread: ThreadId;
  readonly branch: BranchId;
  readonly run: EventId;
};

/** A model that counts its requests and, while `held`, never answers one. */
function counted(
  responses: readonly unknown[],
  held?: Promise<void>,
): Model & { readonly calls: () => number } {
  const model = scriptedModel({ responses: [...responses] });
  let calls = 0;
  const made = {
    ...model,
    calls: () => calls,
    send: async function* (
      ...[request, context, options]: Parameters<Model["send"]>
    ) {
      calls += 1;
      if (held !== undefined) await held;
      yield* model.send(request, context, options);
    },
  };
  markTestKit(made);
  return made;
}

function gate(): Promise<void> {
  const { promise, resolve } = Promise.withResolvers<void>();
  gates.push(resolve);
  return promise;
}

function serve(
  store: Store,
  agents: Parameters<typeof host>[0]["agents"],
): Host {
  const h = host({ store, authenticate, agents });
  hosts.push(h);
  return h;
}

const support = (model: Model) => agent({ name: "support", model });

async function start(h: Host, as: Principal, key = "k-1"): Promise<Run> {
  const started = await h.startRun(
    { agent: "support", input: "Hello" },
    { principal: as, idempotencyKey: key },
  );
  if (!started.ok) throw new Error(started.error.message);
  const { thread_id, branch_id, run_id } = started.value;
  return {
    tenant: as.tenant,
    thread: thread_id,
    branch: branch_id,
    run: run_id,
  };
}

async function read(store: Store, run: Run): Promise<VerifiedLog> {
  const { log } = await openStore(tenantStore(store, run.tenant));
  const read = log.read(run.branch);
  if (!read.ok) throw new Error(read.error.message);
  return read.value;
}

async function events(store: Store, run: Run): Promise<readonly KnownEvent[]> {
  return knownEvents(await read(store, run));
}

const has = async (store: Store, run: Run, type: string): Promise<boolean> =>
  (await events(store, run)).some((e) => e.type === type);

/** The stalled host's lease runs out (its TTL is 30 s): the drills do the same. */
async function expireLeases(store: Store): Promise<void> {
  const { db } = await storeConnection(store);
  db.run("UPDATE leases SET expires_at = 0", []);
}

function texts(all: readonly KnownEvent[]): readonly string[] {
  return all.flatMap((e) =>
    e.type === "model_response"
      ? e.data.content.flatMap((p) => (p.type === "text" ? [p.text] : []))
      : [],
  );
}

describe("a crashed API run resumes on the next host", () => {
  test("its open turn completes without new input, each in its own tenant, and a subscriber sees the end", async () => {
    const store = sqlite(":memory:");
    const stalled = counted([say("late"), say("late")], gate());
    const first = serve(store, { support: support(stalled) });
    const runs = [await start(first, alice), await start(first, eve)];
    for (const run of runs) await until(() => has(store, run, "model_request"));
    const [run] = runs;
    if (run === undefined) throw new Error("two runs");
    const seen = (await read(store, run)).fold.seq;
    await expireLeases(store);

    const answers = counted([say("done"), say("done")]);
    const second = serve(store, { support: support(answers) });
    await second.ready();
    for (const run of runs) {
      await until(() => has(store, run, "turn_completed"), 5_000);
      const all = await events(store, run);
      expect(all.filter((e) => e.type === "user_input")).toHaveLength(1);
      expect(all.find((e) => e.type === "user_input")?.event_id).toBe(run.run);
      expect(texts(all)).toEqual(["done"]);
    }
    expect(answers.calls()).toBe(2);

    const stream = await second.subscribe(run.thread, run.run, {
      principal: alice,
      afterSeq: seen,
    });
    if (!stream.ok) throw new Error(stream.error.message);
    const messages = [];
    for await (const m of stream.value) messages.push(m);
    expect(messages.at(-1)).toMatchObject({
      kind: "result",
      run_id: run.run,
      result: { status: "completed" },
    });
    expect(
      messages.every((m) => m.kind === "result" || m.event.seq > seen),
    ).toBe(true);

    // The stalled host wakes with its answer: the fence refuses it.
    const settled = (await events(store, run)).map((e) => e.event_id);
    for (const open of gates.splice(0)) open();
    await first.stop();
    expect((await events(store, run)).map((e) => e.event_id)).toEqual(settled);
  }, 15_000);

  test("a parked run stays parked", async () => {
    const store = sqlite(":memory:");
    const sent: string[] = [];
    const bot = () =>
      mailer({ responses: [use("send_email", { to: "bob" }, "m1")], sent });
    const first = serve(store, { support: bot() });
    const run = await start(first, alice);
    await until(async () => (await read(store, run)).fold.parked.length > 0);
    await first.stop();
    const parked = (await events(store, run)).map((e) => e.event_id);

    const second = serve(store, { support: bot() });
    await second.ready();
    await hostTicked(second);
    await hostTicked(second);
    expect((await events(store, run)).map((e) => e.event_id)).toEqual(parked);
    expect(sent).toEqual([]);
  }, 10_000);

  test("an effect that began parks and is never sent again", async () => {
    const store = sqlite(":memory:");
    let charged = 0;
    const held = gate();
    const charge = tool({
      name: "charge",
      description: "Charge the card.",
      input: z.object({}),
      runs: "host",
      execute: async () => {
        charged += 1;
        await held;
        return "charged";
      },
    });
    const bot = (model: Model) =>
      agent({
        name: "support",
        model,
        tools: [charge],
        permissions: { allow: ["charge"] },
      });
    const first = serve(store, {
      support: bot(counted([use("charge", {}, "c1"), say("late")])),
    });
    const run = await start(first, alice);
    await until(() => has(store, run, "effect_begin"));
    await expireLeases(store);

    const answers = counted([say("never")]);
    const second = serve(store, { support: bot(answers) });
    await second.ready();
    await until(
      async () => (await read(store, run)).fold.parked.length > 0,
      5_000,
    );
    expect((await read(store, run)).fold.parked.map((p) => p.kind)).toEqual([
      "effect",
    ]);
    expect(charged).toBe(1);
    expect(answers.calls()).toBe(0);
  }, 10_000);

  test("two hosts starting together resume it once", async () => {
    const store = sqlite(":memory:");
    const first = serve(store, {
      support: support(counted([say("late")], gate())),
    });
    const run = await start(first, alice);
    await until(() => has(store, run, "model_request"));
    await expireLeases(store);

    const models = [counted([say("done")]), counted([say("done")])];
    const [a, b] = models.map((m) => serve(store, { support: support(m) }));
    if (a === undefined || b === undefined) throw new Error("two hosts");
    await Promise.all([a.ready(), b.ready()]);
    await until(() => has(store, run, "turn_completed"), 5_000);
    await Promise.all([hostTicked(a), hostTicked(b)]);
    await Promise.all([hostTicked(a), hostTicked(b)]);

    const all = await events(store, run);
    expect(models.reduce((n, m) => n + m.calls(), 0)).toBe(1);
    expect(texts(all)).toEqual(["done"]);
    expect(all.map((e) => e.seq)).toEqual(all.map((_, i) => i + 1));
    const epochs = all.map((e) => e.epoch);
    expect(epochs).toEqual(epochs.toSorted((x, y) => x - y));
  }, 15_000);
});
