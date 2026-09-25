import { z } from "zod";
import { agent, openThread, scriptedModel, type sqlite } from "../../src";
import { openStore, storeConnection } from "../../src/agent/sqlite";
import { sha256Hex } from "../../src/hash";
import { type Cost, canonicalize, ThreadId } from "../../src/log";
import { markTestKit } from "../../src/model/guard";
import type { Model } from "../../src/model/protocol";
import type { Thread } from "../../src/thread";
import { unwrap } from "../store/helpers";

// Shared by the Thread usage and cost tests: priced scripted runs and ways to break their logs.

const PRICE = { input: 3000, output: 15_000 };
/** One scripted response of 10 input and 2 output tokens at PRICE. */
export const ONE: number = 10 * 3000 + 2 * 15_000;
const usage = { input_tokens: 10, output_tokens: 2 };

/** A model script entry. */
export type Reply = {
  readonly content: readonly unknown[];
  readonly stop_reason: string;
  readonly usage: unknown;
};

export function say(text: string): Reply {
  return { content: [{ type: "text", text }], stop_reason: "end_turn", usage };
}

export function spawn(name: string): Reply {
  const input = { agent: name, prompt: "Do it." };
  const call = {
    type: "tool_use",
    call_id: `s-${name}`,
    name: "spawn_agent",
    input,
  };
  return { content: [call], stop_reason: "tool_use", usage };
}

export type Store = ReturnType<typeof sqlite>;

/** A scripted model that declares a price; still test kit, so the request guard allows it. */
export function priced(
  responses: readonly unknown[],
  price: { readonly input: number; readonly output: number } = PRICE,
): Model {
  const base = scriptedModel({ responses });
  const model: Model = {
    ...base,
    info: { ...base.info, limits: { ...base.info.limits, price } },
  };
  markTestKit(model);
  return model;
}

export async function run(
  store: Store,
  model: Model,
  subagents: Parameters<typeof agent>[0]["subagents"] = [],
): Promise<Thread> {
  const result = await agent({ name: "lead", model, subagents }).run("Go.", {
    store,
  });
  return unwrap(await openThread(store, result.thread.id));
}

/** config_hash of an agent with no extensions, skills, memory or sandbox: its canonical pin. */
export function configHash(config: object): string {
  return sha256Hex(unwrap(canonicalize(z.json().parse(config))));
}

/** Rewrites one stored line of `thread`'s main branch so its chain no longer verifies. */
export async function corrupt(
  store: Store,
  thread: ThreadId,
  prompt: string,
): Promise<void> {
  const { db } = await storeConnection(store);
  const { log } = await openStore(store);
  const branch = unwrap(await log.mainBranch(thread));
  await db.transaction((tx) =>
    tx.run(
      "UPDATE events SET line = CAST(replace(CAST(line AS TEXT), ?, 'Edited.') AS BLOB) WHERE branch_id = ? AND seq = 2",
      [prompt, branch],
    ),
  );
}

/** A priced lead whose child "mid" has no price and spawns a priced "leaf". */
export async function unpricedMiddle(store: Store): Promise<Thread> {
  const leaf = agent({ name: "leaf", model: priced([say("Leaf.")]) });
  const mid = agent({
    name: "mid",
    model: scriptedModel({ responses: [spawn("leaf"), say("Mid.")] }),
    subagents: [leaf],
  });
  return run(store, priced([spawn("mid"), say("Done.")]), [mid]);
}

export async function childIds(thread: Thread): Promise<readonly ThreadId[]> {
  return (await thread.children()).map((c) =>
    ThreadId.parse(c.child_thread_id),
  );
}

/** A USD total whose upper bound is its known cost (every attempt here settles). */
export function usd(known: number, exact: boolean): Cost {
  return {
    currency: "USD",
    known_nanos: known,
    upper_bound_nanos: known,
    complete: exact,
    bounded: exact,
  };
}

/** The tree cost of `thread`: Thread.cost({ tree: true }). */
export function tree(thread: Thread): ReturnType<Thread["cost"]> {
  return thread.cost({ tree: true });
}
