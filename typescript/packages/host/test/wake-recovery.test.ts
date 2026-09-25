import { afterEach, describe, expect, test } from "bun:test";
import { agent, type Model, scriptedModel, sqlite } from "@threads/core";
import { knownEvents, storeConnection } from "@threads/core/host";
import { hostTicked } from "../src/host";
import { cleanup, expireLeases, fold, serveAgent, stall } from "./api-kit";
import { alice, say, until } from "./kit";
import { sqlAll } from "./sql";

// A background child a crash stopped still reports (Gate 1 §2.7.3): its pending_wakes row
// survives the crash, and the next host runs the branch on, relaunches the child and records
// its end with the woken that wakes the lead. The run's outcome is the wake turn's answer.

afterEach(cleanup);

const scan = {
  content: [
    {
      type: "tool_use",
      call_id: "c1",
      name: "spawn_agent",
      input: { agent: "scanner", prompt: "Scan.", background: true },
    },
  ],
  stop_reason: "tool_use",
  usage: { input_tokens: 10, output_tokens: 2 },
};

const support = (lead: Model, child: Model, instructions?: string) =>
  agent({
    name: "support",
    model: lead,
    ...(instructions === undefined ? {} : { instructions }),
    subagents: [agent({ name: "scanner", model: child })],
  });

async function rows(
  store: ReturnType<typeof sqlite>,
): Promise<readonly unknown[]> {
  const { db } = await storeConnection(store);
  return await sqlAll(db, "SELECT child_thread_id FROM pending_wakes", []);
}

describe("a pending wake after a crash", () => {
  test("the next host finishes the child and wakes the lead once", async () => {
    const store = sqlite(":memory:");
    const first = serveAgent(
      store,
      support(scriptedModel({ responses: [scan, say("Started.")] }), stall()),
    );
    const started = await first.startRun(
      { agent: "support", input: "Scan in the background." },
      { principal: alice, idempotencyKey: "k-1" },
    );
    if (!started.ok) throw new Error(started.error.message);
    const { branch_id, thread_id, run_id } = started.value;
    const log = async () =>
      knownEvents(await fold(store, alice.tenant, branch_id));
    await until(async () =>
      (await log()).some((e) => e.type === "turn_completed"),
    );
    expect(await rows(store)).toHaveLength(1);
    await expireLeases(store);

    const second = serveAgent(
      store,
      support(
        scriptedModel({ responses: [say("The scan is clean.")] }),
        scriptedModel({ responses: [say("No vulnerable deps.")] }),
      ),
    );
    await second.ready();
    await until(
      async () =>
        (await log()).filter((e) => e.type === "turn_completed").length === 2,
      5_000,
    );
    const all = await log();
    const late = all.filter((e) => e.type === "tool_result_late");
    const woken = all.flatMap((e) => (e.type === "woken" ? [e] : []));
    expect(late).toHaveLength(1);
    expect(woken.map((e) => e.data.causes)).toEqual([
      late.map((e) => e.event_id),
    ]);
    expect(await rows(store)).toEqual([]);

    const stream = await second.subscribe(thread_id, run_id, {
      principal: alice,
    });
    if (!stream.ok) throw new Error(stream.error.message);
    const messages = [];
    for await (const m of stream.value) messages.push(m);
    expect(messages.at(-1)).toMatchObject({
      kind: "result",
      result: { status: "completed", output: "The scan is clean." },
    });
  }, 15_000);
});

describe("a pending wake whose rerun fails for good", () => {
  test("is not run again until its log moves", async () => {
    const store = sqlite(":memory:");
    const first = serveAgent(
      store,
      support(scriptedModel({ responses: [scan, say("Started.")] }), stall()),
    );
    const started = await first.startRun(
      { agent: "support", input: "Scan in the background." },
      { principal: alice, idempotencyKey: "k-1" },
    );
    if (!started.ok) throw new Error(started.error.message);
    const { branch_id } = started.value;
    await until(async () =>
      knownEvents(await fold(store, alice.tenant, branch_id)).some(
        (e) => e.type === "turn_completed",
      ),
    );
    await expireLeases(store);
    // The next host's agent has another config: every rerun of the thread throws.
    const said: string[] = [];
    const error = console.error;
    console.error = (...args: unknown[]) => {
      said.push(args.map(String).join(" "));
    };
    try {
      const second = serveAgent(
        store,
        support(scriptedModel({ responses: [] }), stall(), "Changed."),
      );
      await second.ready();
      for (let i = 0; i < 6; i++) await hostTicked(second);
    } finally {
      console.error = error;
    }
    const retried = said.filter(
      (line) => line.includes(branch_id) && line.includes("not retried"),
    );
    expect(retried).toHaveLength(1);
    expect(await rows(store)).toHaveLength(1);
  }, 15_000);
});
